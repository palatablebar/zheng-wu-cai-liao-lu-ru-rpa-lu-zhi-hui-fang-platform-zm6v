"""HTTP 层集成测试：录制候选化全链路（鉴权 / 422 门禁 / 原子切换）。

用 SQLite 内存库替换 get_db，并覆盖鉴权依赖，不依赖 MySQL/Redis。
"""
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.config import database as db_config
from src.main import app
from src.middlewares import auth as auth_mw
from src.services import audit_service
from src.models.base import Base
from src.models.process import Process
from src.models.process_version import ProcessVersion, RecordingImport
from src.models.step import Step
from src.constants.enums import ProcessStatus, ProcessVersionStatus
from src.services import version_service

ENGINEER = {"sub": "engineer@rpaloop.io", "role": "engineer", "email": "engineer@rpaloop.io"}
ADMIN = {"sub": "admin@rpaloop.io", "role": "admin", "email": "admin@rpaloop.io"}
VIEWER = {"sub": "viewer@rpaloop.io", "role": "viewer", "email": "viewer@rpaloop.io"}


def _recording_payload():
    return {
        "replace": False,
        "steps": [
            {"action": "click", "target_desc": "打开工商变更登记入口",
             "locator_candidates": [{"strategy": "css", "selector": "#change-entry-link"}]},
            {"action": "input", "target_desc": "录入法人姓名", "sample_value": "张法定",
             "locator_candidates": [{"strategy": "css",
                                     "selector": "#form .field-agent input:nth-child(2)",
                                     "desc": "经办人姓名输入框（相邻字段）"}]},
            {"action": "input", "target_desc": "录入经办人姓名", "sample_value": "李经办",
             "locator_candidates": [{"strategy": "css", "selector": "#form .field-agent-name input"}]},
        ],
    }


class RecordingHttpTest(unittest.TestCase):
    def setUp(self):
        # StaticPool + check_same_thread=False：TestClient 跨线程共享同一个内存库
        self.engine = create_engine(
            "sqlite:///:memory:",
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, future=True)

        db = self.Session()
        process = Process(name="工商变更登记", matter_type="change",
                          status=ProcessStatus.PUBLISHED.value, version=1, step_count=3)
        db.add(process)
        db.flush()
        baseline = [
            ("click", "打开工商变更登记入口", "#change-entry-link"),
            ("input", "录入法人姓名", "#form .field-legal input"),
            ("input", "录入经办人姓名", "#form .field-agent input"),
        ]
        for seq, (action, desc, selector) in enumerate(baseline, start=1):
            db.add(Step(process_id=process.id, seq=seq, action=action, target_desc=desc,
                        locator_candidates=[{"strategy": "css", "selector": selector}],
                        locator_scores=[], robustness_score=80.0, risk_level="low",
                        risk_reason="stable", timeout_ms=15000, max_retries=2,
                        depends_on=[], assert_rule=None))
        db.commit()
        version_service.ensure_baseline_snapshot(db, process)
        db.commit()
        self.process_id = process.id
        db.close()

        def override_get_db():
            session = self.Session()
            try:
                yield session
            finally:
                session.close()

        self._db_patch = mock.patch.object(db_config, "SessionLocal", self.Session)
        self._audit_patch = mock.patch.object(audit_service, "SessionLocal", self.Session)
        self._db_patch.start()
        self._audit_patch.start()
        app.dependency_overrides[db_config.get_db] = override_get_db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self._audit_patch.stop()
        self._db_patch.stop()
        self.engine.dispose()

    def _auth(self, user: dict):
        app.dependency_overrides[auth_mw.get_current_user] = lambda: dict(user)

    def _clear_auth(self):
        app.dependency_overrides.pop(auth_mw.get_current_user, None)

    def test_requires_authentication(self):
        self._clear_auth()
        resp = self.client.post(f"/api/processes/{self.process_id}/import", json=_recording_payload())
        self.assertEqual(resp.status_code, 401)

    def test_viewer_cannot_import(self):
        self._auth(VIEWER)
        resp = self.client.post(f"/api/processes/{self.process_id}/import", json=_recording_payload())
        self.assertEqual(resp.status_code, 403)

    def test_full_review_workflow_over_http(self):
        self._auth(ENGINEER)
        # 1) 导入生成候选
        resp = self.client.post(f"/api/processes/{self.process_id}/import", json=_recording_payload())
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertFalse(body["deduplicated"])
        recording = body["recording"]
        self.assertEqual(recording["status"], "pending_review")
        misalign = [item for item in recording["diff_items"] if item["type"] == "suspected_misalign"]
        self.assertTrue(misalign)
        self.assertTrue(any("经办人" in "；".join(item["reasons"]) for item in misalign))
        recording_id = recording["id"]

        # 2) 同一份录制再导一次 → 幂等
        again = self.client.post(f"/api/processes/{self.process_id}/import", json=_recording_payload())
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json()["deduplicated"])
        db = self.Session()
        self.assertEqual(len(db.scalars(select(RecordingImport)).all()), 1)
        db.close()

        # 3) 未确认疑似错位 → 422
        blocked = self.client.post(
            f"/api/processes/recordings/{recording_id}/confirm",
            json={"confirmed_misalign_seqs": [], "note": ""})
        self.assertEqual(blocked.status_code, 422)
        self.assertEqual(blocked.json()["code"], "MISALIGN_NOT_CONFIRMED")
        pending_seqs = blocked.json()["detail"]["pending_seqs"]
        self.assertTrue(pending_seqs)

        # 4) 逐条确认 → 待发布版本；正式版本仍是 v1
        confirmed = self.client.post(
            f"/api/processes/recordings/{recording_id}/confirm",
            json={"confirmed_misalign_seqs": pending_seqs, "note": "已核对页面"})
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["version"]["status"], "pending_publish")
        db = self.Session()
        published = version_service.get_published_version(db, self.process_id)
        self.assertEqual(published.version, 1)
        db.close()

        # 5) engineer 不能发布（仅 admin）
        forbidden = self.client.post(f"/api/processes/{self.process_id}/publish", json={"note": ""})
        self.assertEqual(forbidden.status_code, 403)

        # 6) admin 发布 → 整条流程一次切换
        self._auth(ADMIN)
        publish = self.client.post(f"/api/processes/{self.process_id}/publish",
                                   json={"note": "改版切换"})
        self.assertEqual(publish.status_code, 200, publish.text)
        self.assertTrue(publish.json()["switched"])
        db = self.Session()
        process = db.get(Process, self.process_id)
        self.assertEqual(process.version, 2)
        versions = db.scalars(select(ProcessVersion).where(
            ProcessVersion.process_id == self.process_id)).all()
        statuses = {v.version: v.status for v in versions}
        self.assertEqual(statuses[1], ProcessVersionStatus.SUPERSEDED.value)
        self.assertEqual(statuses[2], ProcessVersionStatus.PUBLISHED.value)
        db.close()

    def test_reject_then_reimport(self):
        self._auth(ENGINEER)
        resp = self.client.post(f"/api/processes/{self.process_id}/import", json=_recording_payload())
        recording_id = resp.json()["recording"]["id"]
        rejected = self.client.post(f"/api/processes/recordings/{recording_id}/reject",
                                    json={"note": "录制不合格"})
        self.assertEqual(rejected.status_code, 200)
        # 驳回后相同内容允许重新导入，形成新批次
        again = self.client.post(f"/api/processes/{self.process_id}/import", json=_recording_payload())
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json()["deduplicated"])
        self.assertNotEqual(again.json()["recording"]["id"], recording_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
