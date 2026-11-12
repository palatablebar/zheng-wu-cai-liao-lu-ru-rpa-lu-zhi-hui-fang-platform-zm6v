"""录制候选化 + 版本切换的端到端验证（SQLite 内存库，不依赖 MySQL/Redis）。

运行：python -m tests.test_recording_workflow
"""
import unittest
from unittest import mock

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config import database as db_config
from src.constants.enums import (
    ProcessStatus, ProcessVersionStatus, RecordingStatus,
)
from src.models.base import Base
from src.models.process import Process
from src.models.process_version import ProcessVersion, RecordingImport
from src.models.step import Step
from src.services import executor, process_service, recording_service, version_service
from src.services.recording_diff import (
    build_recording_diff, content_fingerprint, name_similar, summarize_diff,
)
from src.utils.errors import AppError

ACTOR = {"email": "engineer@rpaloop.io", "role": "engineer"}
ADMIN = {"email": "admin@rpaloop.io", "role": "admin"}


def baseline_steps():
    return [
        {"seq": 1, "action": "click", "target_desc": "打开工商变更登记入口",
         "locator_candidates": [{"strategy": "css", "selector": "#change-entry-link", "desc": "入口"}]},
        {"seq": 2, "action": "input", "target_desc": "录入法人姓名",
         "sample_value": "张法定",
         "locator_candidates": [{"strategy": "css", "selector": "#change-form .field-legal input",
                                 "desc": "法人姓名输入框"}]},
        {"seq": 3, "action": "input", "target_desc": "录入经办人姓名",
         "sample_value": "李经办",
         "locator_candidates": [{"strategy": "css", "selector": "#change-form .field-agent input",
                                 "desc": "经办人姓名输入框"}]},
        {"seq": 4, "action": "input", "target_desc": "录入银行账号",
         "sample_value": "6222000011112222",
         "locator_candidates": [{"strategy": "css", "selector": "#change-form .field-bank input",
                                 "desc": "银行账号输入框"}]},
    ]


def drifted_recording():
    """页面改版后：第 2 步「法人姓名」被录到了相邻的经办人输入框；第 4 步字段改名；银行账号缺失。"""
    return [
        {"action": "click", "target_desc": "打开工商变更登记入口",
         "locator_candidates": [{"strategy": "css", "selector": "#change-entry-link", "desc": "入口"}]},
        {"action": "input", "target_desc": "录入法人姓名", "sample_value": "张法定",
         "locator_candidates": [{"strategy": "css",
                                 "selector": "#change-form .field-agent input:nth-child(2)",
                                 "desc": "经办人姓名输入框（相邻字段）"}]},
        {"action": "input", "target_desc": "录入经办人姓名", "sample_value": "李经办",
         "locator_candidates": [{"strategy": "css", "selector": "#change-form .field-agent-name input",
                                 "desc": "经办人姓名输入框"}]},
        {"action": "input", "target_desc": "录入联系电话", "sample_value": "13800000000",
         "locator_candidates": [{"strategy": "css", "selector": "#change-form .field-phone input",
                                 "desc": "联系电话输入框"}]},
    ]


class RecordingWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:", future=True,
            connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, future=True)
        # 所有服务都从 config.database 引用 SessionLocal
        self._patches = [
            mock.patch.object(db_config, "SessionLocal", self.Session),
            mock.patch.object(executor, "SessionLocal", self.Session),
        ]
        for patch in self._patches:
            patch.start()
        self.db = self.Session()
        # 独立只读会话，避免与业务会话的 identity map 互相污染
        self.read_db = self.Session()

    def tearDown(self):
        self.read_db.close()
        self.db.close()
        for patch in self._patches:
            patch.stop()
        self.engine.dispose()

    def _seed_published_process(self):
        process = Process(name="工商变更登记", matter_type="change", status=ProcessStatus.PUBLISHED.value,
                          version=2, step_count=4, archived_versions=["v1"])
        self.db.add(process)
        self.db.flush()
        for raw in baseline_steps():
            self.db.add(Step(
                process_id=process.id, seq=raw["seq"], action=raw["action"],
                target_desc=raw["target_desc"], locator_candidates=raw["locator_candidates"],
                locator_scores=[], robustness_score=80.0, risk_level="low",
                risk_reason="stable", timeout_ms=15000, max_retries=2, depends_on=[],
                assert_rule=None,
            ))
        self.db.commit()
        version_service.ensure_baseline_snapshot(self.db, process)
        self.db.commit()
        return process

    def test_diff_detects_misalign_added_missing(self):
        diff = build_recording_diff(drifted_recording(), baseline_steps())
        by_type = {}
        for item in diff:
            by_type.setdefault(item["type"], []).append(item)
        self.assertIn("suspected_misalign", by_type)
        misalign = by_type["suspected_misalign"]
        # 第 2 步：名称仍是法人姓名，但定位器落到相邻的经办人字段 → 必须命中疑似错位
        seq2 = next(item for item in misalign if item["seq"] == 2)
        self.assertTrue(any("经办人" in reason for reason in seq2["reasons"]))
        # 银行账号在录制里消失 → 缺失
        missing_names = [item["baseline_field_name"] for item in by_type["missing"]]
        self.assertIn("录入银行账号", missing_names)
        # 联系电话落在旧字段的序号位置：以疑似错位呈现，且原字段已计入缺失
        misalign_names = [item["field_name"] for item in misalign]
        self.assertIn("录入联系电话", misalign_names)
        summary = summarize_diff(diff)
        self.assertTrue(summary["requires_manual_confirmation"])
        self.assertGreaterEqual(summary["suspected_misalign"], 1)
        self.assertGreaterEqual(summary["missing"], 1)

    def test_fingerprint_dedup_and_name_similarity(self):
        left = content_fingerprint(drifted_recording(), False)
        right = content_fingerprint(drifted_recording(), False)
        self.assertEqual(left, right)
        self.assertNotEqual(left, content_fingerprint(drifted_recording(), True))
        self.assertGreaterEqual(name_similar("录入法人姓名", "法人姓名"), 0.9)
        self.assertLess(name_similar("录入法人姓名", "录入银行账号"), 0.72)

    def test_full_import_confirm_publish_switch(self):
        process = self._seed_published_process()
        published_before = version_service.get_published_version(self.db, process.id)
        self.assertEqual(published_before.version, 2)

        # 1) 导入 → 候选批次，不动工作区/正式版本
        result = recording_service.import_recording(
            self.db, ACTOR, process.id, drifted_recording(), replace=False)
        self.assertFalse(result["deduplicated"])
        recording_id = result["recording"]["id"]
        self.assertEqual(result["recording"]["status"], RecordingStatus.PENDING_REVIEW.value)
        working = self.db.scalars(select(Step).where(Step.process_id == process.id)).all()
        self.assertEqual(working[1].target_desc, "录入法人姓名")  # 工作区未被改动
        pending_before_confirm = self.db.scalars(
            select(ProcessVersion).where(
                ProcessVersion.process_id == process.id,
                ProcessVersion.status == ProcessVersionStatus.PENDING_PUBLISH.value)
        ).first()
        self.assertIsNone(pending_before_confirm)

        # 2) 重复导入同一录制 → 幂等，不新增批次/版本
        again = recording_service.import_recording(
            self.db, ACTOR, process.id, drifted_recording(), replace=False)
        self.assertTrue(again["deduplicated"])
        self.assertEqual(again["recording"]["id"], recording_id)
        self.assertEqual(len(self.db.scalars(select(RecordingImport)).all()), 1)

        # 3) 未确认疑似错位就提交 → 422 门禁
        with self.assertRaises(AppError) as ctx:
            recording_service.confirm_recording(self.db, ACTOR, recording_id, [], "")
        self.assertEqual(ctx.exception.code, "MISALIGN_NOT_CONFIRMED")
        self.assertIn(2, ctx.exception.detail["pending_seqs"])

        # 4) 逐条确认疑似错位 → 生成待发布版本，正式发布版本保持不变
        flagged = [item["seq"] for item in result["recording"]["diff_items"]
                   if item["type"] == "suspected_misalign"]
        confirmed = recording_service.confirm_recording(
            self.db, ACTOR, recording_id, flagged, "已核对页面，确认为改版后新位置")
        pending = version_service.get_pending_version(self.db, process.id)
        self.assertIsNotNone(pending)
        self.assertEqual(confirmed["version"]["status"], ProcessVersionStatus.PENDING_PUBLISH.value)
        still_published = version_service.get_published_version(self.db, process.id)
        self.assertEqual(still_published.id, published_before.id)

        # 5) 待发布期间工作区冻结：编辑/再导入都被拒绝
        with self.assertRaises(AppError) as ctx:
            process_service.update_step(self.db, process.id, working[0].id, {"target_desc": "x"})
        self.assertEqual(ctx.exception.code, "PENDING_VERSION_EXISTS")
        other_recording = [{
            "action": "click", "target_desc": "另一份录制的新步骤",
            "locator_candidates": [{"strategy": "css", "selector": "#other-recording-step"}],
        }]
        with self.assertRaises(AppError) as ctx:
            recording_service.import_recording(
                self.db, ACTOR, process.id, other_recording, replace=False)
        self.assertEqual(ctx.exception.code, "PENDING_VERSION_EXISTS")

        # 6) 发布 → 整条流程一次原子切换
        out = process_service.publish_process(self.db, process.id, "改版录制切换")
        self.assertTrue(out["switched"])
        self.db.refresh(process)
        self.assertEqual(process.version, pending.version)
        self.assertEqual(process.status, ProcessStatus.PUBLISHED.value)
        new_published = version_service.get_published_version(self.db, process.id)
        self.assertEqual(new_published.id, pending.id)
        self.assertEqual(new_published.status, ProcessVersionStatus.PUBLISHED.value)
        old = self.read_db.scalars(
            select(ProcessVersion).where(ProcessVersion.id == published_before.id)
        ).first()
        self.assertEqual(old.status, ProcessVersionStatus.SUPERSEDED.value)
        # 工作区步骤同步为候选内容
        working = self.db.scalars(select(Step).where(Step.process_id == process.id)
                                  .order_by(Step.seq)).all()
        self.assertEqual([s.target_desc for s in working],
                         ["打开工商变更登记入口", "录入法人姓名", "录入经办人姓名", "录入联系电话"])

    def test_runs_keep_their_start_version(self):
        process = self._seed_published_process()
        old_version_no = process.version

        class _OldRun:
            id = 99
            process_id = process.id
            process_version = old_version_no

        # 旧版本快照可被运行按版本号取回（含 superseded 状态）
        version = version_service.load_version_for_run(self.read_db, _OldRun())
        self.assertIsNotNone(version)
        self.assertEqual(version.version, old_version_no)
        old_steps = executor.load_run_steps(self.read_db, _OldRun())
        self.assertEqual([s.target_desc for s in old_steps],
                         [s["target_desc"] for s in baseline_steps()])

        # 走完整切换后，旧 run 仍解析到 v2 快照；新 run 解析到新版本
        result = recording_service.import_recording(
            self.db, ACTOR, process.id, drifted_recording(), replace=False)
        flagged = [item["seq"] for item in result["recording"]["diff_items"]
                   if item["type"] == "suspected_misalign"]
        recording_service.confirm_recording(self.db, ACTOR, result["recording"]["id"], flagged, "")
        process_service.publish_process(self.db, process.id, "切换")

        old_version = version_service.load_version_for_run(self.read_db, _OldRun())
        self.assertEqual(old_version.version, old_version_no)
        old_steps_after = executor.load_run_steps(self.read_db, _OldRun())
        self.assertIn("录入银行账号", [s.target_desc for s in old_steps_after])

        class _NewRun:
            id = 100
            process_id = process.id
            process_version = process.version

        new_steps = executor.load_run_steps(self.read_db, _NewRun())
        self.assertNotIn("录入银行账号", [s.target_desc for s in new_steps])
        self.assertIn("录入联系电话", [s.target_desc for s in new_steps])

    def test_reject_pending_allows_reimport(self):
        process = self._seed_published_process()
        result = recording_service.import_recording(
            self.db, ACTOR, process.id, drifted_recording(), replace=False)
        flagged = [item["seq"] for item in result["recording"]["diff_items"]
                   if item["type"] == "suspected_misalign"]
        recording_service.confirm_recording(self.db, ACTOR, result["recording"]["id"], flagged, "")
        pending = version_service.get_pending_version(self.db, process.id)

        process_service.reject_pending_version(self.db, ADMIN, process.id, "复核发现定位仍有问题")
        self.db.refresh(pending)
        self.assertEqual(pending.status, ProcessVersionStatus.REJECTED.value)
        recording = self.db.get(RecordingImport, result["recording"]["id"])
        self.assertEqual(recording.status, RecordingStatus.DISCARDED.value)
        # 正式版本未受影响
        self.assertEqual(version_service.get_published_version(self.db, process.id).version, 2)
        # 驳回后可以重新导入
        again = recording_service.import_recording(
            self.db, ACTOR, process.id, drifted_recording(), replace=False)
        self.assertFalse(again["deduplicated"])

    def test_reject_recording_before_confirm(self):
        process = self._seed_published_process()
        result = recording_service.import_recording(
            self.db, ACTOR, process.id, drifted_recording(), replace=False)
        recording_service.reject_recording(
            self.db, ACTOR, result["recording"]["id"], "录制质量不合格")
        recording = self.db.get(RecordingImport, result["recording"]["id"])
        self.assertEqual(recording.status, RecordingStatus.REJECTED.value)
        self.assertIsNone(version_service.get_pending_version(self.db, process.id))
        with self.assertRaises(AppError):
            recording_service.confirm_recording(
                self.db, ACTOR, result["recording"]["id"], [2], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
