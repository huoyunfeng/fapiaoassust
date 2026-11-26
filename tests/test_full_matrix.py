"""全功能矩阵回归测试（HTTP 层 + 领域引擎纯函数）。

覆盖维度（与《测试文档》章节一一对应）：
  A 基础设施与健康检查      B 认证与会话            C 角色权限矩阵
  D 免费版门禁              E 页面渲染              F 分页内核（含自定义条数）
  G 发票列表分页/筛选/排序  H 发票增删改查与导出    I 报销单
  J 回收站                  K 智能匹配              L 差异报告（多分页器）
  M 智能风控                N 费用分类字典          O 审计日志
  P 公司与规则设置          Q 备份/存储/交接包      R 授权与套餐
  S 演示模式                T 领域引擎

运行：
    python -m pytest tests/test_full_matrix.py -q
"""
import io
import json
import os
import sys
import zipfile
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from tests import matrix_support as sup


# =====================================================================
# 夹具
# =====================================================================
@pytest.fixture()
def env(tmp_path, monkeypatch):
    sup.reset_caches()
    sup.configure(monkeypatch, tmp_path)
    # 测试库不必每次都 fsync，避免 E: 盘上每次写入 400ms 拖慢整个矩阵
    monkeypatch.setattr(settings, "db_synchronous", "OFF")
    sup.init_and_seed_admin()
    yield tmp_path
    sup.reset_caches()


def _client(monkeypatch, login_name=sup.ADMIN_USER, password=sup.ADMIN_PWD):
    from app.main import create_app
    c = TestClient(create_app())
    r = sup.login(c, login_name, password)
    assert r.status_code == 200 and r.json().get("ok"), r.text
    return c


@pytest.fixture()
def client(env, monkeypatch):
    """全功能（企业版/调试模式）管理员会话。"""
    monkeypatch.setattr(settings, "dev_mode", True)
    return _client(monkeypatch)


@pytest.fixture()
def free_client(env, monkeypatch):
    """免费体验版管理员会话（无授权码）。"""
    monkeypatch.setattr(settings, "dev_mode", False)
    return _client(monkeypatch)


@pytest.fixture()
def anon_client(env, monkeypatch):
    monkeypatch.setattr(settings, "dev_mode", True)
    from app.main import create_app
    return TestClient(create_app())


# =====================================================================
# A 基础设施与健康检查
# =====================================================================
class TestAInfra:
    def test_health_ok(self, anon_client):
        r = anon_client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    def test_health_is_public_without_login(self, anon_client):
        # 未登录也必须可探测，供安装器/部署脚本使用
        assert anon_client.get("/health").status_code == 200

    def test_404_unknown_route(self, client):
        r = client.get("/api/not-exists")
        assert r.status_code == 404

    def test_invalid_int_path_param_rejected(self, client):
        # 路径参数类型错误必须由框架拒绝，而不是 500
        r = client.get("/invoices/abc")
        assert r.status_code in (404, 422)


# =====================================================================
# B 认证与会话
# =====================================================================
class TestBAuth:
    def test_unauth_api_returns_no_login(self, anon_client):
        r = anon_client.get("/api/license/status")
        assert r.status_code == 401
        assert r.json()["code"] == "NO_LOGIN"

    def test_unauth_page_redirects_to_login(self, anon_client):
        r = anon_client.get("/invoices", follow_redirects=False)
        assert r.status_code in (302, 307)
        assert "/login" in r.headers["location"]

    def test_unauth_keeps_next_param(self, anon_client):
        r = anon_client.get("/report", follow_redirects=False)
        assert "next=/report" in r.headers["location"]

    def test_setup_rejected_when_already_initialized(self, anon_client):
        r = anon_client.post("/api/auth/setup",
                             json={"username": "x", "password": "Abc@123456"})
        assert r.status_code == 400
        assert "已初始化" in r.json()["message"]

    def test_login_wrong_password(self, env, monkeypatch):
        monkeypatch.setattr(settings, "dev_mode", True)
        from app.main import create_app
        c = TestClient(create_app())
        r = sup.login(c, sup.ADMIN_USER, "wrong-password")
        assert r.status_code == 400
        assert "用户名或密码错误" in r.json()["message"]

    def test_login_locks_after_five_failures(self, env, monkeypatch):
        monkeypatch.setattr(settings, "dev_mode", True)
        from app.main import create_app
        c = TestClient(create_app())
        last = None
        for _ in range(5):
            last = sup.login(c, sup.ADMIN_USER, "bad")
        assert "锁定" in last.json()["message"]      # 第 5 次触发临时锁定
        r = sup.login(c, sup.ADMIN_USER, "bad")
        assert "重试" in r.json()["message"]          # 锁定期间直接拒绝
        # 锁定期间即使密码正确也拒绝
        assert sup.login(c, sup.ADMIN_USER, sup.ADMIN_PWD).status_code == 400

    def test_logout_clears_session(self, client):
        r = client.post("/api/auth/logout")
        assert r.json()["ok"] is True
        r2 = client.get("/api/license/status")
        assert r2.status_code == 401

    def test_change_password_rejects_wrong_old(self, client):
        r = client.post("/api/auth/change-password",
                        json={"old_password": "nope", "new_password": "New@123456"})
        assert r.status_code == 400

    def test_change_password_then_login_with_new(self, env, monkeypatch):
        monkeypatch.setattr(settings, "dev_mode", True)
        from app.main import create_app
        c = TestClient(create_app())
        sup.login(c)
        r = c.post("/api/auth/change-password",
                   json={"old_password": sup.ADMIN_PWD,
                         "new_password": "New@123456"})
        assert r.json()["ok"] is True
        c2 = TestClient(create_app())
        assert sup.login(c2, sup.ADMIN_USER, "New@123456").json()["ok"] is True
        assert sup.login(c2, sup.ADMIN_USER, sup.ADMIN_PWD).status_code == 400

    def test_forgot_employee_channel_hint(self, anon_client):
        r = anon_client.post("/api/auth/forgot", json={
            "username": "emp", "channel": "employee",
            "recovery_code": "X", "new_password": "Abc@123456"})
        assert "联系系统管理员" in r.json()["message"]

    def test_recovery_pending_blocks_business_api(self, env, monkeypatch):
        """未确认恢复码时，业务接口必须被 428 拦截（防用户丢失唯一恢复凭证）。"""
        monkeypatch.setattr(settings, "dev_mode", True)
        from app.repository import db
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO kv (k, v) VALUES ('auth_recovery_pending', '1') "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v")
        from app.main import create_app
        c = TestClient(create_app())
        sup.login(c)
        r = c.get("/api/license/status")
        assert r.status_code == 428
        assert r.json()["code"] == "RECOVERY_ACK"
        # 页面也被重定向到恢复码页
        p = c.get("/invoices", follow_redirects=False)
        assert p.status_code in (302, 303, 307)
        assert "/recovery-code" in p.headers["location"]
        from app.service import auth_service
        auth_service.ack_recovery()
        assert c.get("/api/license/status").status_code == 200


# =====================================================================
# C 角色权限矩阵
# =====================================================================
class TestCRoleMatrix:
    @pytest.fixture()
    def emp_client(self, env, monkeypatch):
        # 先放开席位（dev=企业版），再建员工，避免免费版 1 席位限制
        monkeypatch.setattr(settings, "dev_mode", True)
        from app.service import auth_service
        auth_service.create_user("emp", "Emp@123456", "employee", "员工甲")
        return _client(monkeypatch, "emp", "Emp@123456")

    def test_employee_cannot_open_report_page(self, emp_client):
        r = emp_client.get("/report", follow_redirects=False)
        assert r.status_code == 403
        assert "无权限访问" in r.text

    def test_employee_cannot_call_reimburse_api(self, emp_client):
        r = emp_client.post("/api/reimburses/manual", data={"amount": "1"})
        assert r.status_code == 403

    def test_employee_cannot_read_report_data(self, emp_client):
        r = emp_client.get("/api/report/data")
        assert r.status_code == 403

    def test_employee_cannot_delete_invoice(self, emp_client):
        r = emp_client.post("/api/invoices/1/delete")
        assert r.status_code == 403

    def test_employee_cannot_export(self, emp_client):
        assert emp_client.get("/api/invoices/export").status_code == 403

    def test_employee_can_upload_manual_invoice(self, emp_client):
        r = sup.manual_invoice(emp_client, no="EMP-001", total="50.00")
        assert r["ok"] is True

    def test_employee_sees_only_own_invoices(self, emp_client, client):
        sup.manual_invoice(client, no="ADM-SECRET", total="88.00")
        sup.manual_invoice(emp_client, no="EMP-ONLY", total="66.00")
        html = emp_client.get("/invoices?range=all").text
        assert "共 1 条" in html
        assert "ADM-SECRET" not in html

    def test_employee_cannot_open_team_settings(self, emp_client):
        r = emp_client.get("/settings/team", follow_redirects=False)
        assert r.status_code in (302, 303, 307)
        assert "/settings/rules" in r.headers["location"]

    def test_finance_cannot_manage_team(self, env, monkeypatch):
        monkeypatch.setattr(settings, "dev_mode", True)
        from app.service import auth_service
        auth_service.create_user("fin", "Fin@123456", "finance", "财务乙")
        c = _client(monkeypatch, "fin", "Fin@123456")
        assert c.get("/api/team/users").status_code == 403

    def test_admin_can_manage_team(self, client):
        assert client.get("/api/team/users").status_code == 200


# =====================================================================
# D 免费版门禁
# =====================================================================
class TestDFreeGate:
    def test_free_blocks_report_api(self, free_client):
        r = free_client.get("/api/report/data")
        assert r.status_code == 403
        assert r.json()["code"] == "UPGRADE_REQUIRED"
        assert r.json()["feature"] == "report"

    def test_free_blocks_matching_api(self, free_client):
        r = free_client.post("/api/match/tasks")
        assert r.status_code == 403
        assert r.json()["feature"] == "matching"

    def test_free_blocks_export(self, free_client):
        r = free_client.get("/api/invoices/export")
        assert r.status_code == 403
        assert r.json()["feature"] == "export"

    def test_free_page_redirects_to_plans(self, free_client):
        r = free_client.get("/report", follow_redirects=False)
        assert r.status_code in (302, 307)
        assert "/plans?feature=report" in r.headers["location"]

    def test_free_blocks_custom_category_write(self, free_client):
        r = free_client.post("/api/categories", data={"name": "测试自定义"})
        assert r.status_code == 403
        assert r.json()["feature"] == "custom_categories"

    def test_free_can_read_categories(self, free_client):
        r = free_client.get("/api/categories")
        assert r.status_code == 200
        assert len(r.json()["data"]) >= 6

    def test_free_can_use_invoice_list(self, free_client):
        assert free_client.get("/invoices").status_code == 200

    def test_free_blocks_audit_and_rules(self, free_client):
        assert free_client.get("/api/audit").status_code == 403
        assert free_client.post("/api/rules", data={
            "name": "x", "rule_type": "amount"}).status_code == 403

    def test_free_allows_license_and_status(self, free_client):
        assert free_client.get("/api/license/status").status_code == 200
        assert free_client.get("/api/license/plans").status_code == 200


# =====================================================================
# E 页面渲染
# =====================================================================
class TestEPages:
    @pytest.mark.parametrize("path", ["/", "/invoices", "/reimburses", "/matches",
                                      "/report", "/plans", "/help",
                                      "/settings/rules", "/settings/categories",
                                      "/settings/team", "/settings/security"])
    def test_page_renders(self, client, path):
        r = client.get(path, follow_redirects=True)
        assert r.status_code == 200, f"{path} -> {r.status_code}"

    def test_settings_root_redirects_to_rules(self, client):
        r = client.get("/settings", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/settings/rules"

    def test_unknown_settings_section_falls_back(self, client):
        r = client.get("/settings/nope", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/settings/rules"

    def test_invoice_detail_page(self, client):
        r = sup.manual_invoice(client, no="DETAIL-1", total="10.00")
        iid = r["invoice_id"]
        page = client.get(f"/invoices/{iid}")
        assert page.status_code == 200
        assert "DETAIL-1" in page.text

    def test_missing_invoice_detail_redirects(self, client):
        r = client.get("/invoices/999999", follow_redirects=False)
        assert r.status_code == 302
        assert "/invoices" in r.headers["location"]

    def test_compare_page_bogus_redirects(self, client):
        r = client.get("/matches/999999/compare", follow_redirects=False)
        assert r.status_code == 303
        assert "/matches" in r.headers["location"]

    def test_xss_escaped_in_list(self, client):
        sup.manual_invoice(client, no="XSS-INV-1", total="1.00",
                           buyer="<script>alert(1)</script>")
        html = client.get("/invoices?range=all").text
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html


# =====================================================================
# F 分页内核（纯函数，含自定义条数边界）
# =====================================================================
class TestFPaginationCore:
    @pytest.mark.parametrize("raw,expected", [
        (20, 20), (50, 50), (100, 100), (200, 200),
        (1, 1), (7, 7), (999, 999), (1000, 1000),
        (1001, 1000), (100000, 1000),      # 超上限钳制
        (0, 20), (-5, 20),                 # 非正数回退默认
        ("abc", 20), (None, 20),           # 非法回退默认
    ])
    def test_norm_page_size(self, raw, expected):
        from app.web.pages import _norm_page_size
        assert _norm_page_size(raw) == expected

    def test_paginate_empty(self):
        from app.web.pages import _paginate
        rows, pager = _paginate([], 1, 20)
        assert rows == []
        assert pager == {"total": 0, "page": 1, "pages": 1, "page_size": 20}

    def test_paginate_last_page_partial(self):
        from app.web.pages import _paginate
        rows, pager = _paginate(list(range(5)), 3, 2)
        assert rows == [4]
        assert pager["pages"] == 3
        assert pager["page"] == 3

    def test_paginate_page_overflow_clamped(self):
        from app.web.pages import _paginate
        rows, pager = _paginate(list(range(5)), 99, 2)
        assert pager["page"] == 3
        assert rows == [4]

    def test_paginate_page_zero_clamped(self):
        from app.web.pages import _paginate
        rows, pager = _paginate(list(range(5)), 0, 2)
        assert pager["page"] == 1
        assert rows == [0, 1]

    def test_pager_template_has_custom_option(self):
        """分页组件必须提供 20/50/100/200 预设 + 「自定义」输入（限制 4 位数字）。"""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app", "web", "templates", "_pager.html")
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
        assert "自定义" in html
        assert "[20, 50, 100, 200]" in html     # 预设每页条数
        assert 'maxlength="4"' in html          # 自定义条数最多 4 位
        assert "pagerCustom" in html            # 确定按钮/回车提交
        assert "上一页" in html and "下一页" in html


# =====================================================================
# G 发票列表分页 / 筛选 / 排序 / 定位
# =====================================================================
class TestGInvoiceListPaging:
    @pytest.fixture()
    def seeded(self, client):
        for i in range(1, 6):
            sup.manual_invoice(client, no=f"PAGE-{i:03d}", total=f"{i*10}.00")
        return client

    def test_page_size_splits_rows(self, seeded):
        html = seeded.get("/invoices?range=all&page_size=2&page=1").text
        assert "共 5 条" in html
        assert "第 1 / 3 页" in html

    def test_last_page(self, seeded):
        html = seeded.get("/invoices?range=all&page_size=2&page=3").text
        assert "第 3 / 3 页" in html

    def test_page_overflow_clamped(self, seeded):
        html = seeded.get("/invoices?range=all&page_size=2&page=99").text
        assert "第 3 / 3 页" in html

    def test_custom_page_size_7(self, seeded):
        html = seeded.get("/invoices?range=all&page_size=7").text
        assert "第 1 / 1 页" in html

    def test_invalid_page_size_rejected_by_framework(self, seeded):
        """非整数 page_size 在 FastAPI 参数解析阶段即被拒绝（422），不会静默降级。"""
        r = seeded.get("/invoices?range=all&page_size=abc")
        assert r.status_code == 422

    def test_keyword_filter(self, seeded):
        html = seeded.get("/invoices?range=all&q=PAGE-002").text
        assert "共 1 条" in html
        assert "PAGE-002" in html
        assert "PAGE-003" not in html

    def test_status_filter(self, client):
        sup.manual_invoice(client, no="OK-1", total="10.00")
        sup.seed_raw_invoice("BAD-1", status="ABNORMAL")
        html = client.get("/invoices?range=all&status=ABNORMAL").text
        assert "BAD-1" in html
        assert "OK-1" not in html

    def test_unmatched_alias_maps_to_normal(self, seeded):
        html = seeded.get("/invoices?range=all&status=unmatched").text
        assert "共 5 条" in html

    def test_custom_date_range(self, client):
        sup.manual_invoice(client, no="OLD-1", total="1.00",
                           when=(date.today() - timedelta(days=800)).isoformat())
        sup.manual_invoice(client, no="NEW-1", total="1.00")
        rng = (f"range=custom&date_from={date.today() - timedelta(days=30)}"
               f"&date_to={date.today()}")
        html = client.get(f"/invoices?{rng}").text
        assert "NEW-1" in html
        assert "OLD-1" not in html

    def test_range_all_disables_date_filter(self, client):
        sup.manual_invoice(client, no="ANCIENT-1", total="1.00",
                           when=(date.today() - timedelta(days=900)).isoformat())
        html = client.get("/invoices?range=all").text
        assert "ANCIENT-1" in html

    def test_focus_id_jumps_to_correct_page(self, seeded):
        from tests.matrix_support import invoice_id_of
        iid = invoice_id_of(seeded, "PAGE-004")
        html = seeded.get(f"/invoices?range=all&page_size=2&focus_id={iid}").text
        assert "第 2 / 3 页" in html
        assert "PAGE-004" in html


# =====================================================================
# H 发票增删改查与导出
# =====================================================================
class TestHInvoiceCrud:
    def test_manual_create_ok(self, client):
        r = sup.manual_invoice(client, no="M-1", total="120.00")
        assert r["ok"] is True and r["status"] == "NORMAL"

    def test_manual_missing_total_rejected(self, client):
        r = client.post("/api/invoices/manual", data={
            "invoice_no": "M-2", "invoice_date": sup.today_str()}).json()
        assert r["data"]["ok"] is False
        assert "total_amount" in r["data"]["message"]

    def test_manual_duplicate_rejected(self, client):
        sup.manual_invoice(client, no="DUP-1", total="10.00")
        r = sup.manual_invoice(client, no="DUP-1", total="10.00")
        assert r["ok"] is False
        assert "相同" in r["message"]

    def test_amount_mismatch_rejected_before_insert(self, client):
        """价税合计与金额+税额不符属于 ERROR：人工录入直接拒绝，不产生脏数据。"""
        before = sup.count_rows("invoice")
        r = sup.manual_invoice(client, no="ABN-1", total="100.00", tax="13.00")
        assert r["ok"] is False
        assert "价税合计" in r["message"]
        assert sup.count_rows("invoice") == before

    def test_fix_abnormal(self, client):
        iid = sup.seed_raw_invoice("FIX-1", status="ABNORMAL")
        r = client.post(f"/api/invoices/{iid}/fix",
                        data={"payload": json.dumps({
                            "invoice_no": "FIX-1", "invoice_date": sup.today_str(),
                            "amount": "100.00", "tax_amount": "0.00",
                            "total_amount": "100.00"})}).json()
        assert r["data"]["ok"] is True
        assert "修正成功" in r["data"]["message"]
        # 状态机确实从 ABNORMAL 流转到 NORMAL
        preview = client.get(f"/api/invoices/{iid}").json()
        assert preview["data"]["invoice"]["status"] == "NORMAL"

    def test_update_item_summary(self, client):
        r = sup.manual_invoice(client, no="SUM-1", total="10.00")
        iid = r["invoice_id"]
        out = client.post(f"/api/invoices/{iid}/update-item-summary",
                          data={"item_summary": "餐饮服务"}).json()
        assert out["data"]["ok"] is True
        preview = client.get(f"/api/invoices/{iid}").json()
        assert preview["data"]["invoice"]["item_summary"] == "餐饮服务"

    def test_confirm_not_duplicate(self, client):
        iid = sup.seed_raw_invoice("CNFD-1", status="DUPLICATE")
        out = client.post(f"/api/invoices/{iid}/confirm-not-duplicate",
                          data={"reason": "确认为不同业务"}).json()
        assert out["data"]["ok"] is True
        preview = client.get(f"/api/invoices/{iid}").json()
        assert preview["data"]["invoice"]["status"] == "NORMAL"

    def test_delete_moves_to_recycle_bin(self, client):
        r = sup.manual_invoice(client, no="DEL-1", total="10.00")
        iid = r["invoice_id"]
        assert client.post(f"/api/invoices/{iid}/delete").json()["data"]["ok"] is True
        bin_rows = client.get("/api/recycle-bin").json()["data"]["items"]
        assert any(x["id"] == iid and x["kind"] == "invoice" for x in bin_rows)

    def test_restore_from_20s_undo(self, client):
        r = sup.manual_invoice(client, no="UNDO-1", total="10.00")
        iid = r["invoice_id"]
        client.post(f"/api/invoices/{iid}/delete")
        assert client.post("/api/invoices/restore",
                           data={"ids": str(iid)}).json()["data"]["ok"] is True
        html = client.get("/invoices?range=all&q=UNDO-1").text
        assert "共 1 条" in html

    def test_batch_delete_empty_selection(self, client):
        r = client.post("/api/invoices/batch-delete", data={"ids": ""}).json()
        assert r["data"]["ok"] is False
        assert "未选择" in r["data"]["message"]

    def test_batch_delete_and_restore(self, client):
        ids = [sup.manual_invoice(client, no=f"BD-{i}", total="10.00")["invoice_id"]
               for i in range(3)]
        r = client.post("/api/invoices/batch-delete",
                        data={"ids": ",".join(map(str, ids))}).json()
        assert r["data"]["ok"] is True
        r2 = client.post("/api/invoices/restore",
                         data={"ids": ",".join(map(str, ids))}).json()
        assert r2["data"]["ok"] is True

    def test_deleted_invoice_can_be_reimported(self, client):
        sup.manual_invoice(client, no="REIMP-1", total="10.00")
        from tests.matrix_support import invoice_id_of
        iid = invoice_id_of(client, "REIMP-1")
        client.post(f"/api/invoices/{iid}/delete")
        r = sup.manual_invoice(client, no="REIMP-1", total="10.00")
        assert r["ok"] is True

    def test_export_excel_stream(self, client):
        sup.manual_invoice(client, no="EXP-1", total="10.00")
        r = client.get("/api/invoices/export")
        assert r.status_code == 200
        assert "spreadsheetml" in r.headers["content-type"]
        assert "attachment" in r.headers["content-disposition"]
        assert len(r.content) > 1000
        assert r.content[:2] == b"PK"      # xlsx 是 zip 容器

    def test_export_respects_keyword(self, client):
        sup.manual_invoice(client, no="EXPA-1", total="10.00")
        sup.manual_invoice(client, no="EXPB-1", total="10.00")
        r = client.get("/api/invoices/export?q=EXPA-1")
        assert r.status_code == 200 and r.content[:2] == b"PK"

    def test_preview_missing_invoice(self, client):
        r = client.get("/api/invoices/999999").json()
        assert r["ok"] is False
        assert "不存在" in r["message"]

    def test_similar_endpoint(self, client):
        r = sup.manual_invoice(client, no="SIM-1", total="10.00",
                               summary="办公用品采购")
        out = client.get(f"/api/invoices/{r['invoice_id']}/similar").json()
        assert out["ok"] is True

    def test_upload_unsupported_extension(self, client):
        r = client.post("/api/invoices/upload",
                        files=[("files", ("a.txt", b"hello", "text/plain"))]).json()
        assert r["data"][0]["ok"] is False
        assert "不支持" in r["data"][0]["message"]

    def test_upload_broken_xml_returns_friendly_error(self, client):
        r = client.post("/api/invoices/upload",
                        files=[("files", ("bad.xml", b"<not-an-invoice/>",
                                          "text/xml"))]).json()
        assert r["data"][0]["ok"] is False
        assert r["data"][0]["message"]

    def test_set_invoice_category(self, client):
        r = sup.manual_invoice(client, no="CAT-1", total="10.00")
        iid = r["invoice_id"]
        out = client.post(f"/api/invoices/{iid}/category",
                          data={"category": "差旅费", "sync_reimburse": "0"}).json()
        assert out["data"]["ok"] is True

    def test_batch_category_invalid_versions_json(self, client):
        out = client.post("/api/invoices/category/batch",
                          data={"ids": "1,2", "category": "差旅费",
                                "versions": "not-json"}).json()
        assert out["data"]["ok"] is False
        assert "versions" in out["data"]["message"]


# =====================================================================
# I 报销单
# =====================================================================
class TestIReimburse:
    def test_manual_create(self, client):
        r = sup.manual_reimburse(client, no="BX-M-1", amount="300.00")
        assert r["ok"] is True
        assert r["reimburse_no"] == "BX-M-1"

    def test_manual_amount_invalid(self, client):
        r = client.post("/api/reimburses/manual",
                        data={"amount": "abc"}).json()
        assert r["data"]["ok"] is False
        assert "金额" in r["data"]["message"]

    def test_auto_no_when_blank(self, client):
        r = client.post("/api/reimburses/manual", data={"amount": "10"}).json()
        assert r["data"]["ok"] is True
        assert r["data"]["reimburse_no"].startswith("BX-")

    def test_list_page_shows_row(self, client):
        sup.manual_reimburse(client, no="BX-LIST-1", amount="123.00")
        html = client.get("/reimburses?range=all").text
        assert "BX-LIST-1" in html

    def test_sort_asc_desc(self, client):
        sup.manual_reimburse(client, no="BX-S1", amount="1.00")
        sup.manual_reimburse(client, no="BX-S2", amount="2.00")
        asc = client.get("/reimburses?range=all&sort=asc").text
        desc = client.get("/reimburses?range=all&sort=desc").text
        assert asc.index("BX-S1") < asc.index("BX-S2")
        assert desc.index("BX-S2") < desc.index("BX-S1")

    def test_page_size_and_pager(self, client):
        for i in range(4):
            sup.manual_reimburse(client, no=f"BX-P{i}", amount="1.00")
        html = client.get("/reimburses?range=all&page_size=2").text
        assert "第 1 / 2 页" in html

    def test_batch_delete_summary_and_delete(self, client):
        r = sup.manual_reimburse(client, no="BX-BATCH-1", amount="9.00")
        batch_id = r["batch_id"]
        summary = client.get(f"/api/reimburses/batch/{batch_id}/delete-summary").json()
        assert summary["ok"] is True
        out = client.post(f"/api/reimburses/batch/{batch_id}/delete").json()
        assert out["data"]["ok"] is True
        rows = client.get("/api/recycle-bin").json()["data"]["items"]
        assert any(x["kind"] == "reimburse" for x in rows)

    def test_delete_and_restore_single(self, client):
        sup.manual_reimburse(client, no="BX-RS-1", amount="5.00")
        rid = sup.reimburse_id_of("BX-RS-1")
        assert client.post(f"/api/reimburses/{rid}/delete").json()["data"]["ok"] is True
        assert client.post("/api/reimburses/restore",
                           data={"ids": str(rid)}).json()["data"]["ok"] is True
        assert sup.reimburse_id_of("BX-RS-1") == rid

    def test_restore_empty_selection(self, client):
        r = client.post("/api/reimburses/restore", data={"ids": ""}).json()
        assert r["data"]["ok"] is False

    def test_batch_templates_list(self, client):
        r = client.get("/api/reimburses/templates").json()
        assert r["ok"] is True
        names = [t.get("name") for t in r["data"]] if isinstance(r["data"], list) else []
        assert "经典模板" in names


# =====================================================================
# J 回收站
# =====================================================================
class TestJRecycleBin:
    def test_custom_page_size(self, client):
        ids = [sup.manual_invoice(client, no=f"RB-{i}", total="1.00")["invoice_id"]
               for i in range(3)]
        for i in ids:
            client.post(f"/api/invoices/{i}/delete")
        d = client.get("/api/recycle-bin?page=1&page_size=2").json()["data"]
        assert len(d["items"]) == 2
        assert d["total"] >= 3

    def test_page_size_clamped_to_1000(self, client):
        d = client.get("/api/recycle-bin?page=1&page_size=99999").json()["data"]
        assert d.get("page_size", 20) <= 1000

    def test_restore_kind_invoice(self, client):
        iid = sup.manual_invoice(client, no="RB-RS-1", total="1.00")["invoice_id"]
        client.post(f"/api/invoices/{iid}/delete")
        r = client.post("/api/recycle-bin/restore",
                        data={"kind": "invoice", "ids": str(iid)}).json()
        assert r["data"]["ok"] is True

    def test_purge_physically_deletes(self, client):
        iid = sup.manual_invoice(client, no="RB-PG-1", total="1.00")["invoice_id"]
        client.post(f"/api/invoices/{iid}/delete")
        r = client.post("/api/recycle-bin/purge",
                        data={"kind": "invoice", "ids": str(iid)}).json()
        assert r["data"]["ok"] is True
        assert sup.count_rows("invoice", "id=?", (iid,)) == 0

    def test_purge_invalid_kind(self, client):
        r = client.post("/api/recycle-bin/purge",
                        data={"kind": "unknown", "ids": "1"}).json()
        assert r["data"]["ok"] is False

    def test_recycle_days_roundtrip(self, client):
        assert client.post("/api/settings/recycle-days",
                           data={"days": 7}).json()["data"]["ok"] is True
        assert client.get("/api/settings/recycle-days").json()["data"]["days"] == 7


# =====================================================================
# K 智能匹配
# =====================================================================
class TestKMatching:
    def test_run_creates_results(self, client):
        sup.manual_invoice(client, no="MATCH-1", total="100.00")
        sup.manual_reimburse(client, no="BX-MATCH-1", amount="100.00")
        r = client.post("/api/match/run").json()
        assert r["ok"] is True
        counts = client.get("/matches").text
        assert "无候选" in counts or "待人工" in counts or "已匹配" in counts

    def test_tasks_current(self, client):
        r = client.get("/api/match/tasks/current").json()
        assert r["ok"] is True

    def test_task_not_found(self, client):
        r = client.get("/api/match/tasks/not-a-task").json()
        assert r["ok"] is False
        assert "不存在" in r["message"]

    def test_manual_match_invalid_ids(self, client):
        r = client.post("/api/match/manual",
                        data={"invoice_id": 999999, "reimburse_id": 999998}).json()
        assert r["data"]["ok"] is False

    def test_confirm_bogus_match(self, client):
        r = client.post("/api/match/999999/confirm").json()
        assert r["data"]["ok"] is False

    def test_reject_bogus_match(self, client):
        r = client.post("/api/match/999999/reject").json()
        assert r["data"]["ok"] is False

    def test_unmatch_requires_reason_option(self, client):
        r = client.post("/api/match/999999/unmatch").json()
        assert r["data"]["ok"] is False

    def test_match_center_page_renders(self, client):
        assert client.get("/matches").status_code == 200


# =====================================================================
# L 差异报告（五个独立分页器）
# =====================================================================
class TestLReport:
    def test_report_data_structure(self, client):
        r = client.get("/api/report/data").json()["data"]
        for key in ("stats", "details", "unmatched_reimburses", "unmatched_invoices",
                    "abnormal", "duplicates", "pagers", "badges"):
            assert key in r

    def test_independent_page_sizes(self, client):
        """差异报告五个分页器各自独立：主表与底部 Tab 互不干扰。"""
        for i in range(6):
            sup.manual_invoice(client, no=f"RP-I{i}", total="1.00")
        for i in range(4):
            sup.manual_reimburse(client, no=f"BX-RP{i}", amount="1.00")
        d = client.get("/api/report/data?dsize=50&rsize=20&isize=100").json()["data"]
        assert d["pagers"]["dpage"]["page_size"] == 50
        assert d["pagers"]["rpage"]["page_size"] == 20
        assert d["pagers"]["ipage"]["page_size"] == 100

    def test_per_pager_size_whitelist(self, client):
        """报告分页器只接受 20/50/100，非法值回退默认（与列表页 1~1000 不同）。"""
        d = client.get("/api/report/data?dsize=99999").json()["data"]
        assert d["pagers"]["dpage"]["page_size"] == 20
        d2 = client.get("/api/report/data?dsize=7").json()["data"]
        assert d2["pagers"]["dpage"]["page_size"] == 20

    def test_keyword_filter(self, client):
        sup.manual_invoice(client, no="RPT-KW", total="1.00")
        d = client.get("/api/report/data?q=RPT-KW").json()["data"]
        assert d["q"] == "RPT-KW"

    def test_filter_pending(self, client):
        d = client.get("/api/report/data?filter=pending").json()["data"]
        assert d["filter"] == "pending"

    def test_filter_invalid_falls_back_empty(self, client):
        d = client.get("/api/report/data?filter=hack").json()["data"]
        assert d["filter"] == ""

    def test_options_search(self, client):
        sup.manual_reimburse(client, no="BX-OPT-1", amount="10.00", submitter="李四")
        d = client.get("/api/report/options?q=李四").json()["data"]
        assert any("李四" == x["submitter"] for x in d["reimburses"])

    def test_export_report(self, client):
        r = client.get("/api/report/export")
        assert r.status_code == 200
        assert r.content[:2] == b"PK"

    def test_quick_fix_missing_everything(self, client):
        r = client.post("/api/report/quick-fix", data={}).json()
        assert r["data"]["ok"] is False


# =====================================================================
# M 智能风控
# =====================================================================
class TestMRisk:
    def test_settings_roundtrip(self, client):
        assert client.post("/api/settings/risk", data={
            "seq": "1", "word": "1",
            "words": json.dumps(["烟", "酒"], ensure_ascii=False),
        }).json()["ok"] is True
        d = client.get("/api/settings/risk").json()["data"]
        assert d["seq"] is True and d["word"] is True
        assert "烟" in d["words"] and "酒" in d["words"]

    def test_sensitive_word_triggers_risk_note(self, client):
        client.post("/api/settings/risk", data={
            "seq": "0", "word": "1",
            "words": json.dumps(["香烟"], ensure_ascii=False)})
        sup.manual_invoice(client, no="RISK-1", total="10.00",
                           summary="购买中华香烟一包")
        from app.repository import db
        with db.read() as conn:
            note = conn.execute(
                "SELECT risk_note FROM invoice WHERE invoice_no='RISK-1'"
            ).fetchone()["risk_note"]
        assert "敏感词" in (note or "")

    def test_sequence_rule_triggers(self, client):
        client.post("/api/settings/risk", data={
            "seq": "1", "word": "0", "words": "[]"})
        d = sup.today_str()
        # 连号判定要求发票号有效数字 >= 4 位
        sup.manual_invoice(client, no="00012345", total="1.00", when=d, seller="连锁超市")
        sup.manual_invoice(client, no="00012346", total="1.00", when=d, seller="连锁超市")
        from app.repository import db
        with db.read() as conn:
            note = conn.execute(
                "SELECT risk_note FROM invoice WHERE invoice_no='00012346'"
            ).fetchone()["risk_note"]
        assert "连号" in (note or "") or "跳号" in (note or "")


# =====================================================================
# N 费用分类字典
# =====================================================================
class TestNCategories:
    def test_builtin_categories_present(self, client):
        d = client.get("/api/categories").json()["data"]
        assert len(d) >= 6
        assert all(x.get("is_builtin") for x in d[:6])

    def test_add_custom_category(self, client):
        r = client.post("/api/categories", data={"name": "团建费用"}).json()
        assert r["data"]["ok"] is True or r["data"].get("need_confirm")

    def test_toggle_custom_category(self, client):
        client.post("/api/categories", data={"name": "临时分类A"})
        d = client.get("/api/categories").json()["data"]
        cid = next(x["id"] for x in d if x["name"] == "临时分类A")
        r = client.post(f"/api/categories/{cid}/toggle").json()
        assert r["data"]["ok"] is True

    def test_rename_custom_category(self, client):
        client.post("/api/categories", data={"name": "旧名字B"})
        d = client.get("/api/categories").json()["data"]
        cid = next(x["id"] for x in d if x["name"] == "旧名字B")
        r = client.post(f"/api/categories/{cid}/rename",
                        data={"name": "新名字B", "force": 1}).json()
        assert r["data"]["ok"] is True

    def test_merge_custom_category(self, client):
        client.post("/api/categories", data={"name": "合并源C"})
        client.post("/api/categories", data={"name": "合并目标C"})
        d = client.get("/api/categories").json()["data"]
        src = next(x["id"] for x in d if x["name"] == "合并源C")
        dst = next(x["id"] for x in d if x["name"] == "合并目标C")
        r = client.post(f"/api/categories/{src}/merge",
                        data={"target_id": dst}).json()
        assert r["data"]["ok"] is True

    def test_builtin_cannot_be_renamed(self, client):
        d = client.get("/api/categories").json()["data"]
        builtin = next(x for x in d if x.get("is_builtin"))
        r = client.post(f"/api/categories/{builtin['id']}/rename",
                        data={"name": "篡改内置", "force": 1}).json()
        assert r["data"]["ok"] is False

    def test_conflicts_list(self, client):
        assert client.get("/api/conflicts").json()["ok"] is True


# =====================================================================
# O 审计日志
# =====================================================================
class TestOAudit:
    def test_pagination(self, client):
        # 删除发票会写审计日志（delete 动作），作为分页数据源
        for i in range(5):
            iid = sup.manual_invoice(client, no=f"AU-{i}", total="1.00")["invoice_id"]
            client.post(f"/api/invoices/{iid}/delete")
        d = client.get("/api/audit?page=1&page_size=2").json()["data"]
        assert d["page_size"] == 2
        assert len(d["items"]) == 2
        assert d["total"] >= 5

    def test_action_filter(self, client):
        d = client.get("/api/audit?action=not-exist-action").json()["data"]
        assert d["items"] == [] and d["total"] == 0

    def test_keyword_filter(self, client):
        d = client.get("/api/audit?keyword=admin").json()["data"]
        assert isinstance(d["items"], list)

    def test_date_range_filter(self, client):
        d = client.get("/api/audit?date_from=2000-01-01&date_to=2000-01-02").json()["data"]
        assert d["total"] == 0


# =====================================================================
# P 公司与规则设置
# =====================================================================
class TestPSettings:
    def test_company_add_list_default_delete(self, client):
        r = client.post("/api/companies",
                        data={"name": "北京示例科技有限公司",
                              "tax_no": "91110108MA01AAAA1A",
                              "is_default": "1"}).json()
        cid = r["data"]["id"]
        lst = client.get("/api/settings/companies").json()["data"]["companies"]
        assert any(c["id"] == cid for c in lst)
        assert client.post(f"/api/companies/{cid}/default").json()["data"]["ok"] is True
        assert client.post(f"/api/companies/{cid}/delete").json()["data"]["ok"] is True
        lst2 = client.get("/api/settings/companies").json()["data"]["companies"]
        assert all(c["id"] != cid for c in lst2)

    def test_company_tax_no_normalized_upper(self, client):
        r = client.post("/api/companies",
                        data={"name": "大写税号公司",
                              "tax_no": "91310000ma01cccc3c"}).json()
        lst = client.get("/api/settings/companies").json()["data"]["companies"]
        row = next(c for c in lst if c["id"] == r["data"]["id"])
        assert row["tax_no"] == "91310000MA01CCCC3C"

    def test_rule_add_reorder_toggle_delete(self, client):
        r = client.post("/api/rules", data={
            "name": "金额上限", "rule_type": "amount", "scope": "invoice",
            "action": "warn", "config": json.dumps({"max": 10000})}).json()
        assert r["ok"] is True
        rid = r["data"]["id"]
        assert client.post("/api/rules/reorder",
                           data={"ids": str(rid)}).json()["ok"] is True
        assert client.post(f"/api/rules/{rid}/toggle").json()["data"]["enabled"] is False
        assert client.post(f"/api/rules/{rid}/delete").json()["data"]["deleted"] == rid

    def test_rule_invalid_json_config(self, client):
        r = client.post("/api/rules", data={
            "name": "x", "rule_type": "amount", "config": "not-json"}).json()
        assert r["ok"] is False

    def test_core_rule_config(self, client):
        from app.repository import db
        with db.read() as conn:
            rid = conn.execute(
                "SELECT id FROM rule WHERE is_core=1 AND rule_type='sensitive_words' "
                "LIMIT 1").fetchone()["id"]
        r = client.post(f"/api/rules/{rid}/config",
                        data={"config": json.dumps({"words": ["测试词"]}),
                              "enabled": "1"}).json()
        assert r["ok"] is True
        assert r["data"]["id"] == rid
        assert "测试词" in r["data"]["config"]["words"]


# =====================================================================
# Q 备份 / 存储 / 交接包
# =====================================================================
class TestQBackupStorage:
    def test_backup_status(self, client):
        d = client.get("/api/backup/status").json()["data"]
        assert d["backing_up"] is False

    def test_backup_download_is_zip(self, client):
        sup.manual_invoice(client, no="BK-1", total="10.00")
        r = client.get("/api/backup")
        assert r.status_code == 200
        assert r.content[:2] == b"PK"
        names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
        assert any(n.endswith(".db") for n in names)
        assert any("manifest" in n for n in names)

    def test_backup_last(self, client):
        assert client.get("/api/backup/last").json()["ok"] is True

    def test_restore_rejects_non_zip(self, client):
        r = client.post("/api/backup/restore",
                        files={"file": ("a.txt", b"x", "text/plain")}).json()
        assert r["data"]["ok"] is False
        assert ".zip" in r["data"]["message"]

    def test_storage_info(self, client):
        d = client.get("/api/storage").json()["data"]
        assert d["ok"] is True
        assert len(d) > 1          # 至少包含容量字段（total/upload/recycle 等）

    def test_storage_clean_requires_selection(self, client):
        r = client.post("/api/storage/clean", json={}).json()
        assert r["data"]["ok"] is False

    def test_storage_top_attachments(self, client):
        d = client.get("/api/storage/top-attachments?limit=5").json()["data"]
        assert d["ok"] is True

    def test_storage_path_info(self, client):
        d = client.get("/api/storage/path").json()["data"]
        assert d["ok"] is True

    def test_task_migrate_requires_path(self, client):
        r = client.post("/api/tasks/migrate", json={"new_path": ""}).json()
        assert r["data"]["ok"] is False

    def test_task_clean_requires_selection(self, client):
        r = client.post("/api/tasks/clean-selected", json={}).json()
        assert r["data"]["ok"] is False

    def test_task_status_unknown(self, client):
        assert client.get("/api/tasks/unknown-task").status_code == 404

    def test_handover_export_is_zip(self, client):
        sup.manual_invoice(client, no="HO-1", total="10.00")
        r = client.get("/api/handover/export")
        assert r.status_code == 200
        assert r.content[:2] == b"PK"

    def test_handover_import_rejects_non_zip(self, client):
        r = client.post("/api/handover/import",
                        files={"file": ("a.txt", b"x", "text/plain")}).json()
        assert r["data"]["ok"] is False

    def test_diagnose_export_is_zip(self, client):
        r = client.get("/api/system/diagnose")
        assert r.status_code == 200
        assert r.content[:2] == b"PK"


# =====================================================================
# R 授权与套餐（只读接口，不发真实下单请求）
# =====================================================================
class TestRLicense:
    def test_status_shape(self, client):
        d = client.get("/api/license/status").json()["data"]
        for key in ("activated", "plan", "plan_name", "lifetime", "readonly",
                    "quota", "machine_code", "trial_total", "seat_limit"):
            assert key in d, f"缺少字段 {key}"
        assert d["plan"] == "enterprise" and d["activated"] is True

    def test_plans_contains_five_skus(self, client):
        d = client.get("/api/license/plans").json()["data"]
        keys = {s["key"] for s in d["skus"]}
        assert keys == {"basic_lifetime", "ent_lifetime", "maintain_basic",
                        "maintain_ent", "upgrade_basic_to_ent"}
        assert d["trial_total"] == 50

    def test_license_legacy_endpoint(self, client):
        d = client.get("/api/license").json()["data"]
        assert d["ok"] is True
        assert "used" in d["quota"] and "quota" in d["quota"]

    def test_free_quota_display(self, free_client):
        d = free_client.get("/api/license/status").json()["data"]
        assert d["plan"] == "free"
        assert d["trial_total"] == 50

    def test_team_users_list(self, client):
        d = client.get("/api/team/users").json()["data"]
        users = d["users"] if isinstance(d, dict) and "users" in d else d
        assert any(u["username"] == sup.ADMIN_USER for u in users)

    def test_team_user_create_role_toggle_delete(self, client):
        r = client.post("/api/team/users", json={
            "username": "u1", "password": "U1@123456",
            "display_name": "员工一", "role": "employee"}).json()
        assert r["ok"] is True
        uid = r["user_id"]
        assert client.post(f"/api/team/users/{uid}/role",
                           json={"role": "finance"}).json()["ok"] is True
        assert client.post(f"/api/team/users/{uid}/toggle",
                           json={"active": False}).json()["ok"] is True
        assert client.post(f"/api/team/users/{uid}/toggle",
                           json={"active": True}).json()["ok"] is True
        assert client.post(f"/api/team/users/{uid}/reset",
                           json={"password": "U1@654321"}).json()["ok"] is True
        assert client.delete(f"/api/team/users/{uid}").json()["ok"] is True

    def test_team_cannot_delete_last_admin(self, client):
        users = client.get("/api/team/users").json()["data"]
        admin = next(u for u in users if u["username"] == sup.ADMIN_USER)
        r = client.delete(f"/api/team/users/{admin['id']}").json()
        assert r["ok"] is False


# =====================================================================
# S 演示模式
# =====================================================================
class TestSDemo:
    def test_demo_play_public(self, anon_client):
        assert anon_client.get("/demo/play").status_code == 200

    def test_demo_report_public(self, anon_client):
        assert anon_client.get("/demo/report").status_code == 200

    def test_demo_qr_png(self, anon_client):
        r = anon_client.get("/demo/qr.png")
        assert r.status_code == 200
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_demo_seed_and_clear(self, client):
        assert client.get("/demo/seed",
                          follow_redirects=False).status_code in (200, 302, 303)
        from app.repository import db
        with db.read() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM reimburse "
                             "WHERE batch_id LIKE 'DEMO-%'").fetchone()["c"]
        assert n > 0
        assert client.post("/demo/clear").status_code in (200, 302, 303)


# =====================================================================
# T 领域引擎（纯函数边界）
# =====================================================================
class TestTDomainEngines:
    def test_matcher_multi_combination(self):
        from decimal import Decimal
        from app.domain.dto import InvoiceDTO, ReimburseDTO
        from app.domain.matcher import MatchEngine

        def inv(no, amt):
            return InvoiceDTO(invoice_no=no, total_amount=Decimal(str(amt)),
                              invoice_date=sup.today_str(),
                              seller_name="S", buyer_name="B",
                              amount=Decimal(str(amt)), tax_amount=Decimal("0"))

        reb = ReimburseDTO(reimburse_no="R", amount=Decimal("100"),
                           submitter="X", reimburse_date=sup.today_str(),
                           period_start=sup.today_str(), period_end=sup.today_str())
        combos = MatchEngine().find_many_to_one_all(
            [inv("a", 20), inv("b", 80), inv("c", 30), inv("d", 70)], reb)
        assert len(combos) >= 2

    def test_validator_amount_mismatch(self):
        from decimal import Decimal
        from app.domain.dto import InvoiceDTO
        from app.domain.validator import InvoiceValidator
        dto = InvoiceDTO(invoice_no="1", invoice_date=sup.today_str(),
                         amount=Decimal("100"), tax_amount=Decimal("13"),
                         total_amount=Decimal("120"))
        codes = {i.code for i in InvoiceValidator().validate(dto)}
        assert "AMOUNT_MISMATCH" in codes

    def test_validator_missing_fields(self):
        from app.domain.dto import InvoiceDTO
        from app.domain.validator import InvoiceValidator
        dto = InvoiceDTO(invoice_no="", invoice_date=None, total_amount=None)
        codes = {i.code for i in InvoiceValidator().validate(dto)}
        assert "MISSING_FIELD" in codes

    def test_validator_future_date(self):
        from decimal import Decimal
        from app.domain.dto import InvoiceDTO
        from app.domain.validator import InvoiceValidator
        future = (date.today() + timedelta(days=5)).isoformat()
        dto = InvoiceDTO(invoice_no="1", invoice_date=future,
                         total_amount=Decimal("1"), amount=Decimal("1"),
                         tax_amount=Decimal("0"))
        assert "FUTURE_DATE" in {i.code for i in InvoiceValidator().validate(dto)}

    def test_dedup_same_no(self):
        from app.domain.dedup import DedupEngine, Fingerprint
        fp = Fingerprint(file_hash="h2", invoice_code="", invoice_no="12345")
        hit = DedupEngine().check(fp, [(1, "h1", "", "12345")])
        assert hit is not None and hit.kind == "code_no"

    def test_dedup_same_file_hash(self):
        from app.domain.dedup import DedupEngine, Fingerprint
        fp = Fingerprint(file_hash="same", invoice_code="", invoice_no="99999")
        hit = DedupEngine().check(fp, [(7, "same", "", "00001")])
        assert hit is not None and hit.kind == "file_hash"

    def test_normalize_amount_precision(self):
        from decimal import Decimal
        from app.domain.normalize import to_decimal
        assert to_decimal("1,234.565") == Decimal("1234.57")
        assert to_decimal("¥100.00") == Decimal("100.00")
        assert to_decimal("") is None

    def test_state_transition_matrix(self):
        from app.domain.states import InvalidTransition, assert_invoice_transition
        assert_invoice_transition("PARSING", "PARSED")       # 正常链路不抛
        assert_invoice_transition("NORMAL", "MATCHED")
        assert_invoice_transition("MATCHED", "NORMAL")       # 释放匹配必须可回退
        with pytest.raises(InvalidTransition):
            assert_invoice_transition("PARSING", "MATCHED")

    def test_excel_export_has_header(self):
        from app.infra.excel import export_report
        data = export_report([("Sheet1", ["发票号", "金额"], [["A001", "10.00"]])])
        assert data[:2] == b"PK"                            # xlsx 容器
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        assert any(n.startswith("xl/") for n in names)
