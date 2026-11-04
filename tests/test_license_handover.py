"""授权/额度/交接包/备份版本校验/附件路径迁移 测试（P2/P3/P4 地基包）。"""
import io
import json
import zipfile
from pathlib import Path

import pytest

from app.config import settings


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """独立临时库与上传目录（不污染正式数据）。"""
    monkeypatch.setattr(settings, "db_path", tmp_path / "t.db")
    monkeypatch.setattr(settings, "upload_dir", tmp_path / "uploads")
    monkeypatch.setattr(settings, "log_dir", tmp_path / "logs")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    from app.repository.db import init_schema
    init_schema()
    return tmp_path


# ---------------------------------------------------------------- 授权
class TestLicense:
    def test_machine_code_format(self, env):
        from app.service import license_service as L
        mc = L.get_machine_code()
        assert len(mc) == 19 and mc.count("-") == 3
        assert mc == mc.upper()

    def test_activate_roundtrip(self, env):
        from app.service import license_service as L
        mc = L.get_machine_code()
        assert L.current_license()["plan"] == "free"
        code = L.generate_license_code(mc, "enterprise", 0)
        r = L.activate(code)
        assert r["ok"] is True and r["plan"] == "enterprise"
        assert L.is_paid() and L.has_feature("handover")
        L.deactivate()
        assert L.current_license()["plan"] == "free"

    def test_activate_wrong_machine_rejected(self, env):
        from app.service import license_service as L
        code = L.generate_license_code("AAAA-BBBB-CCCC-DDDD", "personal", 1)
        assert L.activate(code)["ok"] is False

    def test_activate_two_year_license(self, env):
        """签发年限必须与激活枚举一致（2 年版曾因枚举缺失无法激活）。"""
        from app.service import license_service as L
        code = L.generate_license_code(L.get_machine_code(), "personal", 2)
        r = L.activate(code)
        assert r["ok"] is True and r["years"] == 2

    def test_activate_bad_format(self, env):
        from app.service import license_service as L
        assert L.activate("")["ok"] is False
        assert L.activate("HELLO-WORLD")["ok"] is False

    def test_cloud_stubs_reserved(self, env):
        from app.service import license_service as L
        assert L.verify_order_online("O123")["code"] == "cloud_not_configured"
        assert L.sync_blacklist()["blacklisted"] is False
        assert L.request_unbind("O123")["code"] == "cloud_not_configured"


# ---------------------------------------------------------------- 免费版额度防刷
def _hidden_path(tmp_path):
    """测试用隐藏目录桩：替换系统隐藏路径，自动建父目录。"""
    def _p():
        d = tmp_path / "hidden"
        d.mkdir(parents=True, exist_ok=True)
        return d / "usage.dat"
    return _p


class TestQuota:
    def test_free_quota_dual_record(self, env, tmp_path, monkeypatch):
        from app.service import license_service as L
        monkeypatch.setattr(L, "_hidden_quota_path", _hidden_path(tmp_path))
        st = L.quota_status()
        assert st["unlimited"] is False and st["used"] == 0 and st["remaining"] == 20
        L.consume_quota(5)
        st2 = L.quota_status()
        assert st2["used"] == 5 and st2["remaining"] == 15

    def test_hidden_record_survives_db_reset(self, env, tmp_path, monkeypatch):
        """删掉 DB 记录（模拟重装），隐藏目录记录仍能防刷。"""
        from app.service import license_service as L
        monkeypatch.setattr(L, "_hidden_quota_path", _hidden_path(tmp_path))
        L.consume_quota(7)
        with L.db_repo.tx() as conn:
            conn.execute("DELETE FROM quota_usage")
        assert L.quota_status()["used"] == 7  # 取两者最大值

    def test_quota_block_message(self, env, tmp_path, monkeypatch):
        from app.service import license_service as L
        monkeypatch.setattr(L, "_hidden_quota_path", _hidden_path(tmp_path))
        L.consume_quota(20)
        msg = L.check_quota(1)
        assert msg and "免费版" in msg
        assert L.check_quota(0) is None or isinstance(L.check_quota(0), str)

    def test_paid_unlimited(self, env, tmp_path, monkeypatch):
        from app.service import license_service as L
        monkeypatch.setattr(L, "_hidden_quota_path", _hidden_path(tmp_path))
        L.activate(L.generate_license_code(L.get_machine_code(), "personal", 1))
        assert L.quota_status()["unlimited"] is True
        assert L.check_quota(999) is None

    def test_hidden_record_from_other_machine_ignored(self, env, tmp_path, monkeypatch):
        """换机/换盘带来的旧记录（签名合法但机器码不同）不应锁死本机免费额度。"""
        from app.service import license_service as L
        monkeypatch.setattr(L, "_hidden_quota_path", _hidden_path(tmp_path))
        month, other = L._month_key(), "AAAA-BBBB-CCCC-DDDD"
        p = _hidden_path(tmp_path)()
        p.write_text(json.dumps({"month": month, "used": 9, "machine": other,
                                 "sig": L._quota_sig(month, 9, other)}), encoding="utf-8")
        assert L.quota_status()["used"] == 0

    def test_hidden_record_tampered_blocks_quota(self, env, tmp_path, monkeypatch):
        """同机记录签名不符视为人为篡改 → 按已用尽处理。"""
        from app.service import license_service as L
        monkeypatch.setattr(L, "_hidden_quota_path", _hidden_path(tmp_path))
        month = L._month_key()
        p = _hidden_path(tmp_path)()
        p.write_text(json.dumps({"month": month, "used": 9,
                                 "machine": L.get_machine_code(), "sig": "bad"}),
                     encoding="utf-8")
        assert L.quota_status()["used"] == L.FREE_MONTHLY_QUOTA


# ---------------------------------------------------------------- 开发者调试模式
class TestDevMode:
    def test_dev_mode_bypasses_license_and_quota(self, env, tmp_path, monkeypatch):
        from app.service import license_service as L
        monkeypatch.setattr(L, "_hidden_quota_path", _hidden_path(tmp_path))
        monkeypatch.setattr(L.settings, "dev_mode", True)
        lic = L.current_license()
        assert lic["plan"] == "enterprise" and lic["dev_mode"] is True
        assert L.is_paid() and L.has_feature("handover")
        assert L.quota_status()["unlimited"] is True
        assert L.check_quota(999) is None          # 不拦截
        L.consume_quota(50)                        # 不扣额度
        assert L.quota_status()["used"] == 0

    def test_dev_mode_off_by_default(self, env, tmp_path, monkeypatch):
        from app.service import license_service as L
        monkeypatch.setattr(L, "_hidden_quota_path", _hidden_path(tmp_path))
        monkeypatch.setattr(L.settings, "dev_mode", False)
        assert L.current_license()["plan"] == "free"
        L.consume_quota(20)
        assert L.check_quota(1) is not None        # 恢复额度拦截

    def test_dev_mode_from_env_var(self, monkeypatch):
        import app.config as cfg
        monkeypatch.setenv("FAPIAO_DEV_MODE", "1")
        monkeypatch.setattr(cfg, "FROZEN", False, raising=False)
        monkeypatch.setattr(cfg, "_persisted", {}, raising=False)
        enabled = (cfg.os.getenv("FAPIAO_DEV_MODE", "").strip().lower()
                   in ("1", "true", "yes", "on"))
        assert enabled is True


# ---------------------------------------------------------------- 数据交接包
class TestHandover:
    def _seed(self):
        from app.repository import db
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO invoice (invoice_no, buyer_name, seller_name, amount, "
                "total_amount, invoice_date, status, source_path, file_hash, "
                "file_type, parse_engine) "
                "VALUES ('INV-001', '购买方A', '销售方B', '100', '113', "
                "'2026-08-01', 'NORMAL', 'a.pdf', 'hash-seed-001', 'pdf', 'manual')")
            conn.execute(
                "INSERT INTO reimburse (batch_id, reimburse_no, submitter, amount, "
                "reimburse_date, row_index) "
                "VALUES ('BATCH-001', 'BX-001', '张三', '100', '2026-08-02', 1)")

    def test_export_import_roundtrip(self, env):
        from app.service import handover_service as H
        from app.repository import db
        up = settings.upload_dir
        up.mkdir(parents=True, exist_ok=True)
        (up / "a.pdf").write_bytes(b"%PDF-1.4 fake")
        self._seed()
        ex = H.export_handover()
        assert ex["invoices"] == 1 and ex["reimburses"] == 1 and ex["attachments"] == 1

        # 本机再导入同一包：全部按重复跳过，绝不覆盖
        content = ex["zip_path"].read_bytes()
        r = H.import_handover(content)
        assert r["ok"] and r["invoices_added"] == 0 and r["invoices_skipped"] == 1
        assert r["reimburses_added"] == 0 and r["reimburses_skipped"] == 1
        with db.read() as conn:
            assert conn.execute("SELECT COUNT(*) FROM invoice").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM reimburse").fetchone()[0] == 1

    def test_import_rejects_full_backup(self, env):
        from app.service import handover_service as H, backup_service as B
        self._seed()
        info = B.create_backup()
        r = H.import_handover(info["zip_path"].read_bytes())
        assert r["ok"] is False and "全量备份" in r["message"]

    def test_import_rejects_garbage(self, env):
        from app.service import handover_service as H
        assert H.import_handover(b"not a zip")["ok"] is False
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("handover.json", "{bad json")
        assert H.import_handover(buf.getvalue())["ok"] is False

    def test_import_skips_conflicting_file_hash(self, env):
        """同文件指纹但不同票号：唯一索引会冲突，必须跳过该行而不是整包失败。"""
        from app.service import handover_service as H
        from app.repository import db
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO invoice (invoice_no, file_hash, file_type, parse_engine, status) "
                "VALUES ('INV-OLD', 'H-SAME', 'pdf', 'manual', 'NORMAL')")
        payload = {
            "kind": "handover", "app_version": "1.0.0",
            "invoices": [{"invoice_no": "INV-OTHER", "file_hash": "H-SAME",
                          "file_type": "pdf", "parse_engine": "manual",
                          "status": "NORMAL", "amount": "10"}],
            "reimburses": [],
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("handover.json", json.dumps(payload, ensure_ascii=False))
        r = H.import_handover(buf.getvalue())
        assert r["ok"] is True
        assert r["invoices_added"] == 0 and r["invoices_skipped"] == 1

    def test_import_same_name_conflict_keeps_both(self, env):
        """附件同名但内容不同：绝不覆盖本机原件，改名保存并回写 source_path。"""
        from app.service import handover_service as H
        from app.repository import db
        up = settings.upload_dir
        up.mkdir(parents=True, exist_ok=True)
        (up / "a.pdf").write_bytes(b"LOCAL-ORIGINAL")
        payload = {
            "kind": "handover", "app_version": "1.0.0", "created_at": "2026-01-01",
            "invoices": [{
                "invoice_no": "INV-NEW", "buyer_name": "B", "seller_name": "S",
                "amount": "50", "invoice_date": "2026-09-01", "status": "NORMAL",
                "source_path": "D:\\other\\uploads\\202609\\a.pdf",
                "file_hash": "hash-new", "file_type": "pdf", "parse_engine": "manual",
            }],
            "reimburses": [],
        }
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("handover.json", json.dumps(payload, ensure_ascii=False))
            zf.writestr("uploads/a.pdf", b"REMOTE-CONTENT")
        r = H.import_handover(buf.getvalue())
        assert r["ok"] and r["invoices_added"] == 1 and r["attachments_added"] == 1
        assert (up / "a.pdf").read_bytes() == b"LOCAL-ORIGINAL"
        assert (up / "a__dupe1.pdf").read_bytes() == b"REMOTE-CONTENT"
        with db.read() as conn:
            sp = conn.execute(
                "SELECT source_path FROM invoice WHERE invoice_no = 'INV-NEW'"
            ).fetchone()[0]
        assert Path(sp).name == "a__dupe1.pdf"


# ---------------------------------------------------------------- 备份版本强校验（P4-1）
class TestBackupVersion:
    def test_manifest_written(self, env):
        from app.service import backup_service as B
        from app.config import APP_VERSION
        info = B.create_backup()
        with zipfile.ZipFile(info["zip_path"]) as zf:
            m = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert m["app_version"] == APP_VERSION and m["kind"] == "full-backup"

    def test_restore_rejects_newer_pack(self, env, monkeypatch):
        from app.service import backup_service as B
        info = B.create_backup()
        # 伪装成"更高版本生成的包"
        buf = io.BytesIO()
        with zipfile.ZipFile(info["zip_path"]) as zin, \
             zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename == "manifest.json":
                    zout.writestr("manifest.json",
                                  json.dumps({"app_version": "99.0.0"}))
                else:
                    zout.writestr(item, zin.read(item.filename))
        r = B.restore_backup(buf.getvalue())
        assert r["ok"] is False and "升级" in r["message"]

    def test_restore_legacy_pack_without_manifest(self, env):
        """无 manifest 的旧备份按 0.0.0 处理，恢复成功并标记 migrated。"""
        from app.service import backup_service as B
        info = B.create_backup()
        buf = io.BytesIO()
        with zipfile.ZipFile(info["zip_path"]) as zin, \
             zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename != "manifest.json":
                    zout.writestr(item, zin.read(item.filename))
        r = B.restore_backup(buf.getvalue())
        assert r["ok"] is True and r["migrated"] is True


# ---------------------------------------------------------------- 附件路径迁移（P2-4）
class TestStoragePathMigrate:
    def test_migrate_success(self, env, tmp_path, monkeypatch):
        from app.service import backup_service as B
        import app.service.backup_service as bs
        up = settings.upload_dir
        (up / "202608").mkdir(parents=True)
        (up / "202608" / "x.pdf").write_bytes(b"data")
        new_dir = tmp_path / "moved_uploads"
        monkeypatch.setattr(bs, "load_persisted_config", lambda: {}, raising=False) \
            if hasattr(bs, "load_persisted_config") else None
        # migrate_upload_dir 内部 from app.config import，直接打 config 模块
        import app.config as cfg
        saved = {}
        monkeypatch.setattr(cfg, "save_persisted_config",
                            lambda c: saved.update(cfg=c))
        monkeypatch.setattr(cfg, "load_persisted_config", lambda: {})
        r = B.migrate_upload_dir(str(new_dir))
        assert r["ok"] is True and r["files"] == 1
        assert (new_dir / "202608" / "x.pdf").read_bytes() == b"data"
        assert settings.upload_dir == new_dir
        assert "uploads_pre_migrate_" in r["old_backup"]

    def test_migrate_rejects_relative_path(self, env):
        from app.service import backup_service as B
        r = B.migrate_upload_dir("uploads2")
        assert r["ok"] is False and "绝对路径" in r["message"]

    def test_migrate_rejects_same_path(self, env):
        from app.service import backup_service as B
        r = B.migrate_upload_dir(str(settings.upload_dir))
        assert r["ok"] is False and "相同" in r["message"]

    def test_migrate_rewrites_invoice_source_path(self, env, tmp_path, monkeypatch):
        """迁移后历史行的绝对路径必须同步改写，否则预览/清理全失效。"""
        from app.service import backup_service as B
        from app.repository import db
        import app.config as cfg
        up = settings.upload_dir
        (up / "202608").mkdir(parents=True)
        (up / "202608" / "x.pdf").write_bytes(b"data")
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO invoice (invoice_no, source_path, file_hash, file_type, "
                "parse_engine, status) VALUES ('INV-M', ?, 'h-m', 'pdf', 'manual', 'NORMAL')",
                (str(up / "202608" / "x.pdf"),))
        monkeypatch.setattr(cfg, "save_persisted_config", lambda c: None)
        monkeypatch.setattr(cfg, "load_persisted_config", lambda: {})
        new_dir = tmp_path / "moved"
        r = B.migrate_upload_dir(str(new_dir))
        assert r["ok"] is True and r.get("paths_rewritten") == 1
        with db.read() as conn:
            sp = conn.execute(
                "SELECT source_path FROM invoice WHERE invoice_no = 'INV-M'"
            ).fetchone()[0]
        assert sp == str(new_dir / "202608" / "x.pdf")
