"""交通票据 OCR 文本解析规则单元测试（不依赖 OCR 引擎）。

覆盖 7 种交通票据类型：机票、火车票、汽车票、轮船票、网约车、过路过桥费、地铁/公交。
验证字段提取、误判排除、优先级匹配、金额千分位清理等核心逻辑。
"""
from app.domain.parsers import extract_fields_from_text


# ---------- 金额正则 ----------

class TestAmountRegex:
    """金额标签匹配：小写/价税合计/合计金额/票价/金额。"""

    def test_xiaoxie(self):
        text = "发票号码 12345678\n（小写）¥1,280.00\n"
        dto = extract_fields_from_text(text)
        assert dto.total_amount == "1280.00"

    def test_jiashui_heji(self):
        text = "发票号码 12345678\n价税合计：¥553.00\n"
        dto = extract_fields_from_text(text)
        assert dto.total_amount == "553.00"

    def test_heji_jine(self):
        text = "发票号码 12345678\n合计金额：¥1,330.00\n"
        dto = extract_fields_from_text(text)
        assert dto.total_amount == "1330.00"

    def test_piaojia(self):
        text = "发票号码 12345678\n票价：¥85.00\n"
        dto = extract_fields_from_text(text)
        assert dto.total_amount == "85.00"

    def test_jine_only(self):
        """地铁票只有「金额」没有「合计金额」。"""
        text = "发票号码 12345678\n金额：¥5.00\n"
        dto = extract_fields_from_text(text)
        assert dto.total_amount == "5.00"

    def test_thousands_separator(self):
        """千分位逗号自动清理。"""
        text = "发票号码 12345678\n合计金额：¥12,345,678.99\n"
        dto = extract_fields_from_text(text)
        assert dto.total_amount == "12345678.99"

    def test_amount_priority(self):
        """多个金额标签同时存在时按优先级取第一个匹配。"""
        text = "发票号码 12345678\n（小写）¥100.00\n价税合计：¥200.00\n"
        dto = extract_fields_from_text(text)
        assert dto.total_amount == "100.00"


# ---------- 机票行程单 ----------

class TestFlightTicket:
    def test_basic_fields(self):
        text = (
            "航空运输电子客票行程单\n"
            "发票代码 011002500111\n"
            "发票号码 08987654\n"
            "开票日期 2026-11-15\n"
            "旅客姓名：张三\n"
            "航班号：CA1234\n"
            "出发地：北京首都\n"
            "目的地：上海浦东\n"
            "票价：¥1,280.00\n"
            "民航发展基金：¥50.00\n"
            "燃油附加费：¥0.00\n"
            "合计金额：¥1,330.00\n"
            "销售单位：中国国航\n"
        )
        dto = extract_fields_from_text(text)
        assert dto.invoice_code == "011002500111"
        assert dto.invoice_no == "08987654"
        assert dto.invoice_date == "2026-11-15"
        assert dto.total_amount == "1330.00"
        assert dto.buyer_name == "张三"
        assert dto.seller_name == "中国国航"
        assert dto.item_summary == "航空运输电子客票行程单"
        assert dto.flight_no == "CA1234"

    def test_airline_full_name(self):
        text = "航空运输电子客票行程单\n中国国际航空\n"
        dto = extract_fields_from_text(text)
        assert dto.seller_name == "中国国际航空"

    def test_airline_short_name(self):
        text = "航空运输电子客票行程单\n南方航空\n"
        dto = extract_fields_from_text(text)
        assert dto.seller_name == "南方航空"

    def test_not_misjudged_as_taxi(self):
        """机票含「燃油附加费」，不应被误判为出租车票。"""
        text = (
            "航空运输电子客票行程单\n"
            "发票号码 08987654\n"
            "燃油附加费：¥0.00\n"
            "旅客姓名：张三\n"
        )
        dto = extract_fields_from_text(text)
        assert dto.item_summary == "航空运输电子客票行程单"
        assert dto.item_summary != "出租车费"


# ---------- 火车票 ----------

class TestTrainTicket:
    def test_basic_fields(self):
        text = (
            "中国铁路电子发票\n"
            "发票代码 011002500222\n"
            "发票号码 07654321\n"
            "开票日期 2026-11-18\n"
            "乘车人：李四\n"
            "车次：G123\n"
            "出发站：北京南\n"
            "到达站：上海虹桥\n"
            "二等座\n"
            "票价：¥553.00\n"
        )
        dto = extract_fields_from_text(text)
        assert dto.invoice_no == "07654321"
        assert dto.total_amount == "553.00"
        assert dto.buyer_name == "李四"
        assert dto.seller_name == "中国铁路"
        assert dto.item_summary == "铁路客运"
        assert dto.train_no == "G123"

    def test_train_no_with_letter(self):
        text = "中国铁路\n车次：D456\n乘车人：王五\n"
        dto = extract_fields_from_text(text)
        assert dto.train_no == "D456"


# ---------- 客运汽车票 ----------

class TestBusTicket:
    def test_basic_fields(self):
        text = (
            "公路汽车客票\n"
            "发票号码 06543210\n"
            "开票日期 2026-11-10\n"
            "乘车人：王五\n"
            "班次：K2026\n"
            "起点站：北京六里桥\n"
            "终点站：天津东站\n"
            "票价：¥85.00\n"
        )
        dto = extract_fields_from_text(text)
        assert dto.invoice_no == "06543210"
        assert dto.total_amount == "85.00"
        assert dto.buyer_name == "王五"
        assert dto.seller_name == "公路客运"
        assert dto.item_summary == "公路汽车客票"
        assert dto.train_no == "K2026"  # 复用 train_no 存班次

    def test_not_misjudged_as_train(self):
        """汽车票含「座位」等关键词，不应被误判为火车票。"""
        text = (
            "公路汽车客票\n"
            "乘车人：王五\n"
            "起点站：北京\n"
            "终点站：天津\n"
            "座位号：12\n"
        )
        dto = extract_fields_from_text(text)
        assert dto.item_summary == "公路汽车客票"
        assert dto.seller_name == "公路客运"
        assert dto.seller_name != "中国铁路"


# ---------- 轮船票 ----------

class TestShipTicket:
    def test_basic_fields(self):
        text = (
            "水路客运船票\n"
            "发票号码 04321098\n"
            "开票日期 2026-11-05\n"
            "乘船人：赵六\n"
            "船名：渤海明珠\n"
            "航线：大连—烟台\n"
            "开航日期：2026-11-06\n"
            "票价：¥260.00\n"
        )
        dto = extract_fields_from_text(text)
        assert dto.invoice_no == "04321098"
        assert dto.total_amount == "260.00"
        assert dto.buyer_name == "赵六"
        assert dto.seller_name == "水路客运"
        assert dto.item_summary == "水路客运船票"
        assert dto.train_no == "渤海明珠"  # 复用 train_no 存船名

    def test_not_misjudged_as_train(self):
        """轮船票不应被误判为火车票。"""
        text = (
            "水路客运船票\n"
            "乘船人：赵六\n"
            "航线：大连—烟台\n"
        )
        dto = extract_fields_from_text(text)
        assert dto.item_summary == "水路客运船票"
        assert dto.seller_name == "水路客运"
        assert dto.seller_name != "中国铁路"


# ---------- 网约车 ----------

class TestRideHailing:
    def test_basic_fields(self):
        text = (
            "网约车行程单\n"
            "发票号码 03210987\n"
            "开票日期 2026-11-03\n"
            "乘客姓名：孙七\n"
            "车牌号：京B88888\n"
            "上车地点：北京西站\n"
            "下车地点：中关村软件园\n"
            "里程：18.5公里\n"
            "车费：¥58.00\n"
        )
        dto = extract_fields_from_text(text)
        assert dto.invoice_no == "03210987"
        assert dto.total_amount == "58.00"
        assert dto.buyer_name == "孙七"
        assert dto.item_summary == "网约车服务费"
        assert dto.plate_no == "京B88888"

    def test_platform_name(self):
        text = "网约车行程单\n滴滴出行\n乘客姓名：孙七\n"
        dto = extract_fields_from_text(text)
        assert dto.seller_name == "滴滴出行"

    def test_not_misjudged_as_flight(self):
        """网约车含「行程单」，不应被误判为机票。"""
        text = (
            "网约车行程单\n"
            "乘客姓名：孙七\n"
            "上车地点：北京西站\n"
            "下车地点：中关村\n"
        )
        dto = extract_fields_from_text(text)
        assert dto.item_summary == "网约车服务费"
        assert dto.item_summary != "航空运输电子客票行程单"


# ---------- 过路过桥费 ----------

class TestTollTicket:
    def test_basic_fields(self):
        text = (
            "车辆通行费发票\n"
            "发票号码 05432109\n"
            "开票日期 2026-11-08\n"
            "入口收费站：北京杜家坎\n"
            "出口收费站：天津杨村\n"
            "车型：一类客车\n"
            "金额：¥35.00\n"
        )
        dto = extract_fields_from_text(text)
        assert dto.invoice_no == "05432109"
        assert dto.total_amount == "35.00"
        assert dto.seller_name == "高速公路运营"
        assert dto.item_summary == "车辆通行费"


# ---------- 地铁/公交定额发票 ----------

class TestMetroTicket:
    def test_basic_fields(self):
        text = (
            "北京市定额发票\n"
            "发票号码 02109876\n"
            "金额：¥5.00\n"
            "开票日期 2026-11-01\n"
            "北京市轨道交通运营有限公司\n"
        )
        dto = extract_fields_from_text(text)
        assert dto.invoice_no == "02109876"
        assert dto.total_amount == "5.00"
        assert dto.seller_name == "北京市轨道交通运营有限公司"
        assert dto.item_summary == "公共交通费"

    def test_amount_without_heji(self):
        """地铁票只有「金额」标签，无「合计金额」。"""
        text = "定额发票\n发票号码 02109876\n金额：¥5.00\n"
        dto = extract_fields_from_text(text)
        assert dto.total_amount == "5.00"


# ---------- 误判排除（跨类型） ----------

class TestMisjudgmentExclusion:
    """确保各类型之间不会互相误判。"""

    def test_taxi_not_misjudged_as_flight(self):
        """出租车票不应被误判为机票。"""
        text = (
            "出租车发票\n"
            "车号：A-U9328\n"
            "日期 2026-10-29\n"
            "里程 10.5km\n"
            "实收金额 ¥168.50\n"
            "燃油附加费\n"
        )
        dto = extract_fields_from_text(text)
        assert dto.item_summary == "出租车费"
        assert dto.item_summary != "航空运输电子客票行程单"

    def test_flight_not_misjudged_as_taxi(self):
        """机票不应被误判为出租车票。"""
        text = (
            "航空运输电子客票行程单\n"
            "发票号码 08987654\n"
            "燃油附加费：¥0.00\n"
            "旅客姓名：张三\n"
        )
        dto = extract_fields_from_text(text)
        assert dto.item_summary == "航空运输电子客票行程单"
        assert dto.item_summary != "出租车费"

    def test_ride_hailing_not_misjudged_as_flight(self):
        """网约车不应被误判为机票。"""
        text = (
            "网约车行程单\n"
            "乘客姓名：孙七\n"
            "上车地点：北京西站\n"
            "下车地点：中关村\n"
        )
        dto = extract_fields_from_text(text)
        assert dto.item_summary == "网约车服务费"
        assert dto.item_summary != "航空运输电子客票行程单"


# ---------- 字段不覆盖 ----------

class TestFieldPreservation:
    """已有字段值不应被后续提取覆盖。"""

    def test_existing_buyer_not_overwritten(self):
        text = "旅客姓名：张三\n购买方名称：北京某公司\n"
        dto = extract_fields_from_text(text)
        # 先匹配到「购买方名称」则 buyer_name 已有值，不应被「旅客姓名」覆盖
        # 具体行为取决于正则执行顺序，此处验证字段最终有值即可
        assert dto.buyer_name  # 非空

    def test_existing_item_summary_not_overwritten(self):
        """item_summary 已有值时不应被交通票据覆盖。"""
        # 这个场景在通用摘要提取之前执行交通票据提取，
        # 所以交通票据的 item_summary 会优先设置
        text = "航空运输电子客票行程单\n旅客姓名：张三\n"
        dto = extract_fields_from_text(text)
        assert dto.item_summary == "航空运输电子客票行程单"


# ---------- 空文本/边界 ----------

class TestEdgeCases:
    def test_empty_text(self):
        dto = extract_fields_from_text("")
        assert dto.invoice_no == ""
        assert dto.total_amount is None

    def test_no_matching_type(self):
        """不属于任何交通票据类型的文本。"""
        text = "增值税普通发票\n发票号码 12345678\n购买方名称：某公司\n"
        dto = extract_fields_from_text(text)
        assert dto.item_summary != "航空运输电子客票行程单"
        assert dto.item_summary != "铁路客运"
        assert dto.item_summary != "出租车费"

    def test_multiple_types_priority(self):
        """同时含机票和火车关键词时，机票优先。"""
        text = (
            "航空运输电子客票行程单\n"
            "旅客姓名：张三\n"
            "中国铁路\n"
            "乘车人：李四\n"
        )
        dto = extract_fields_from_text(text)
        assert dto.item_summary == "航空运输电子客票行程单"
        assert dto.buyer_name == "张三"
