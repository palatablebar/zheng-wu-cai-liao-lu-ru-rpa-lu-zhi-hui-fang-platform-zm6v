"""执行器按运行版本快照回放的验证：发布切换不影响进行中/历史运行。"""
import unittest
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config import database as db_config
from src.constants.enums import ProcessStatus, RunStatus
from src.models.base import Base
from src.models.material import Material
from src.models.process import Process
from src.models.process_version import ProcessVersion
from src.models.run import Run
from src.models.run_step import RunStep
from src.models.step import Step
from src.services import executor, process_service, recording_service, version_service
from src.services.version_service import snapshot_step_from_raw
from src.utils.credit_code import validate_credit_code

ACTOR = {"email": "engineer@rpaloop.io", "role": "engineer"}


def _steps_v1():
    return [
        {"seq": 1, "action": "input", "target_desc": "录入法人姓名", "sample_value": "张法定",
         "locator_candidates": [{"strategy": "css", "selector": "#form .field-legal input"}]},
        {"seq": 2, "action": "input", "target_desc": "录入银行账号", "sample_value": "6222000011112222",
         "locator_candidates": [{"strategy": "css", "selector": "#form .field-bank input"}]},
    ]


def _steps_v2_drifted():
    # 改版后法人姓名被录到经办人输入框；银行账号字段消失，新增联系电话
    return [
        {"seq": 1, "action": "input", "target_desc": "录入法人姓名", "sample_value": "张法定",
         "locator_candidates": [{"strategy": "css", "selector": "#form .field-agent input"}]},
        {"seq": 2, "action": "input", "target_desc": "录入联系电话", "sample_value": "13800000000",
         "locator_candidates": [{"strategy": "css", "selector": "#form .field-phone input"}]},
    ]


class ExecutorVersionTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:", future=True,
            connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, future=True)
        self._patches = [
            mock.patch.object(db_config, "SessionLocal", self.Session),
            mock.patch.object(executor, "SessionLocal", self.Session),
        ]
        for patch in self._patches:
            patch.start()
        self.db = self.Session()

    def tearDown(self):
        self.db.close()
        for patch in self._patches:
            patch.stop()
        self.engine.dispose()

    def _seed(self):
        process = Process(name="工商变更登记", matter_type="change",
                          status=ProcessStatus.PUBLISHED.value, version=1, step_count=2)
        self.db.add(process)
        self.db.flush()
        for raw in _steps_v1():
            self.db.add(Step(process_id=process.id, seq=raw["seq"], action=raw["action"],
                             target_desc=raw["target_desc"],
                             locator_candidates=raw["locator_candidates"], locator_scores=[],
                             robustness_score=80.0, risk_level="low", risk_reason="stable",
                             timeout_ms=15000, max_retries=2, depends_on=[], assert_rule=None))
        valid, reason = validate_credit_code("91320500MA1XY2K3AJ")
        material = Material(company_name="苏州恒瑞精密机械有限公司", credit_code="91320500MA1XY2K3AJ",
                            contact="测试", attachments=[], validity_date=None, missing_flags=[],
                            credit_valid=valid, credit_reason=reason)
        self.db.add(material)
        self.db.commit()
        version_service.ensure_baseline_snapshot(self.db, process)
        self.db.commit()
        return process, material

    def _make_run(self, process, material, version_no):
        run = Run(run_code=f"RUN-TEST-{version_no}", process_id=process.id, process_version=version_no,
                  material_id=material.id, trigger="dry_run", status=RunStatus.QUEUED.value,
                  total_steps=2, active_lock=None)
        self.db.add(run)
        self.db.commit()
        return run

    def test_execute_uses_bound_snapshot_after_switch(self):
        process, material = self._seed()

        # 旧 run 绑定 v1
        old_run = self._make_run(process, material, 1)
        executor._execute(self.db, old_run, dry_run=True)
        self.db.refresh(old_run)
        self.assertEqual(old_run.status, RunStatus.SUCCEEDED.value)
        old_run_steps = self.db.scalars(
            select(RunStep).where(RunStep.run_id == old_run.id).order_by(RunStep.seq)).all()
        self.assertEqual([rs.locator_used for rs in old_run_steps],
                         ["#form .field-legal input", "#form .field-bank input"])

        # 导入改版录制 → 确认 → 发布切换到 v2
        result = recording_service.import_recording(
            self.db, ACTOR, process.id, _steps_v2_drifted(), replace=False)
        flagged = [item["seq"] for item in result["recording"]["diff_items"]
                   if item["type"] == "suspected_misalign"]
        self.assertTrue(flagged)  # 法人姓名落到经办人字段必须被标出
        recording_service.confirm_recording(self.db, ACTOR, result["recording"]["id"], flagged, "")
        process_service.publish_process(self.db, process.id, "改版切换")
        self.db.refresh(process)
        self.assertEqual(process.version, 2)

        # 新 run 绑定 v2，回放使用新快照定位器
        new_run = self._make_run(process, material, 2)
        executor._execute(self.db, new_run, dry_run=True)
        self.db.refresh(new_run)
        self.assertEqual(new_run.status, RunStatus.SUCCEEDED.value)
        new_run_steps = self.db.scalars(
            select(RunStep).where(RunStep.run_id == new_run.id).order_by(RunStep.seq)).all()
        self.assertEqual([rs.locator_used for rs in new_run_steps],
                         ["#form .field-agent input", "#form .field-phone input"])

        # 旧 run 的历史记录没有被新版本改写
        old_run_steps_after = self.db.scalars(
            select(RunStep).where(RunStep.run_id == old_run.id).order_by(RunStep.seq)).all()
        self.assertEqual([rs.locator_used for rs in old_run_steps_after],
                         ["#form .field-legal input", "#form .field-bank input"])

        # 版本快照不可变：v1 内容仍是旧定位器
        v1 = self.db.scalars(select(ProcessVersion).where(
            ProcessVersion.process_id == process.id, ProcessVersion.version == 1)).first()
        self.assertEqual(
            [s["locator_candidates"][0]["selector"] for s in v1.steps_snapshot],
            ["#form .field-legal input", "#form .field-bank input"],
        )

    def test_manual_edit_publish_creates_new_snapshot(self):
        process, material = self._seed()
        # 在编辑器手工调整工作区步骤（无录制候选）
        step1 = self.db.scalars(select(Step).where(
            Step.process_id == process.id, Step.seq == 1)).first()
        step1.target_desc = "录入法人姓名（手工修订）"
        self.db.commit()

        out = process_service.publish_process(self.db, process.id, "手工调整")
        self.assertTrue(out["switched"])
        self.db.refresh(process)
        self.assertEqual(process.version, 2)
        versions = self.db.scalars(select(ProcessVersion).where(
            ProcessVersion.process_id == process.id).order_by(ProcessVersion.version)).all()
        self.assertEqual([v.status for v in versions], ["superseded", "published"])
        self.assertIn("手工修订", versions[1].steps_snapshot[0]["target_desc"])
        # v1 快照不可变，旧运行仍引用旧名称
        self.assertEqual(versions[0].steps_snapshot[0]["target_desc"], "录入法人姓名")

    def test_unchanged_workspace_publish_does_not_bump_version(self):
        process, material = self._seed()
        out = process_service.publish_process(self.db, process.id, "无改动再发布")
        self.assertFalse(out["switched"])
        self.db.refresh(process)
        self.assertEqual(process.version, 1)

    def test_snapshot_step_normalization(self):
        raw = {"action": "input", "target_desc": "字段", "sample_value": "样例",
               "locator_candidates": [{"strategy": "css", "selector": "#x"}], "timeout_ms": "8000"}
        snap = snapshot_step_from_raw(raw, 3)
        self.assertEqual(snap["seq"], 3)
        self.assertEqual(snap["timeout_ms"], 8000)
        self.assertEqual(snap["sample_value"], "样例")
        self.assertEqual(snap["depends_on"], [])

    def test_board_resolves_steps_from_bound_snapshot(self):
        from src.services import intervention_service, run_service
        process, material = self._seed()

        # 先跑一个 v1 的运行并产生 RunStep
        old_run = self._make_run(process, material, 1)
        executor._execute(self.db, old_run, dry_run=True)

        # 切换到 v2
        result = recording_service.import_recording(
            self.db, ACTOR, process.id, _steps_v2_drifted(), replace=False)
        flagged = [item["seq"] for item in result["recording"]["diff_items"]
                   if item["type"] == "suspected_misalign"]
        recording_service.confirm_recording(self.db, ACTOR, result["recording"]["id"], flagged, "")
        process_service.publish_process(self.db, process.id, "改版切换")

        # 运行详情/报告必须用旧 run 绑定的 v1 快照还原字段，而不是当前工作区的 v2 名称
        detail = run_service.run_detail(self.db, old_run.id)
        names = [step["target_desc"] for step in detail["run_steps"]]
        self.assertIn("录入银行账号", names)
        self.assertNotIn("录入联系电话", names)

        # 看板快照在版本切换后仍能正常生成（步骤解析走运行绑定版本，不抛错）
        from src.services import board_service
        board_service.board_snapshot(self.db)

        # 视图映射：旧 run 按 seq 取到 v1 字段
        view_map = version_service.step_view_map_for_run(self.db, old_run)
        self.assertEqual(view_map[2].target_desc, "录入银行账号")
        self.assertEqual(view_map[1].target_desc, "录入法人姓名")

        # 接管服务在没有工单时也不应因步骤解析报错
        self.assertEqual(intervention_service.list_interventions(self.db), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
