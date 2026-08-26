"""ROI 计算器：商家版 / 达人版。"""
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication,
    QAbstractSpinBox,
    QButtonGroup,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    """避免鼠标滚轮误触修改 ROI 参数。"""

    def wheelEvent(self, event):
        event.ignore()


class ROICalculatorWidget(QWidget):
    """紧凑型 ROI 计算器，区分商家投放口径和达人带货口径。"""

    MODE_MERCHANT = "merchant"
    MODE_CREATOR = "creator"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("roiCalculatorPage")
        self.mode = self.MODE_MERCHANT
        self._result_labels = {}
        self._last_metrics = {}
        self._build_ui()
        self._apply_styles()
        self.recalculate()

    def _build_ui(self):
        page_layout = QVBoxLayout(self)
        page_layout.setContentsMargins(22, 16, 22, 18)
        page_layout.setSpacing(0)

        shell = QFrame()
        shell.setObjectName("roiShell")
        shell.setMaximumWidth(920)
        shell.setMinimumWidth(860)
        page_layout.addWidget(shell, 0, Qt.AlignLeft | Qt.AlignTop)

        root = QVBoxLayout(shell)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(10)
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        title = QLabel("ROI出价计算器")
        title.setObjectName("pageTitle")
        self.subtitle_label = QLabel("商家版：按商品成本、退款、扣点和税费，计算保本 ROI 与出价档位。")
        self.subtitle_label.setObjectName("mutedText")
        self.subtitle_label.setWordWrap(True)
        title_box.addWidget(title)
        title_box.addWidget(self.subtitle_label)
        header.addLayout(title_box, 1)

        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.merchant_btn = self._mode_button("商家版", self.MODE_MERCHANT)
        self.creator_btn = self._mode_button("达人版", self.MODE_CREATOR)
        header.addWidget(self.merchant_btn)
        header.addWidget(self.creator_btn)
        root.addLayout(header)

        self.stack = QStackedWidget()
        root.addWidget(self.stack)
        self.stack.addWidget(self._build_merchant_page())
        self.stack.addWidget(self._build_creator_page())

        action_row = QHBoxLayout()
        action_row.addStretch()
        reset_btn = QPushButton("恢复默认")
        reset_btn.clicked.connect(self.reset_defaults)
        copy_btn = QPushButton("复制结果")
        copy_btn.clicked.connect(self.copy_results)
        action_row.addWidget(reset_btn)
        action_row.addWidget(copy_btn)
        root.addLayout(action_row)

    def _mode_button(self, text, mode):
        btn = QPushButton(text)
        btn.setObjectName("modeButton")
        btn.setCheckable(True)
        btn.setMinimumWidth(88)
        btn.clicked.connect(lambda checked=False, m=mode: self.switch_mode(m))
        self.mode_group.addButton(btn)
        if mode == self.MODE_MERCHANT:
            btn.setChecked(True)
        return btn

    def _build_merchant_page(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        left_pane = QWidget()
        left_layout = QVBoxLayout(left_pane)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)

        self.price_spin = self._money_spin(99.0)
        self.product_cost_spin = self._money_spin(30.0)
        self.freight_cost_spin = self._money_spin(5.0)
        self.refund_rate_spin = self._percent_spin(5.0)
        self.platform_rate_spin = self._percent_spin(5.0)
        self.vat_rate_spin = self._percent_spin(13.0)
        self.ad_cost_spin = self._money_spin(0.0)
        self.target_roi_spin = self._ratio_spin(2.5)

        fields = [
            ("售价", self.price_spin, "商品成交单价，单位元"),
            ("产品成本", self.product_cost_spin, "进货/生产成本"),
            ("运费成本", self.freight_cost_spin, "发货、包装、仓储等单件成本"),
            ("退款率", self.refund_rate_spin, "百分比格式：5 = 5%"),
            ("平台扣点", self.platform_rate_spin, "百分比格式：5 = 5%"),
            ("增值税", self.vat_rate_spin, "百分比格式：13 = 13%"),
            ("广告花费", self.ad_cost_spin, "单件分摊投流成本，0 = 只看保本"),
            ("目标 ROI", self.target_roi_spin, "例如 2.5 表示 ROI 2.5"),
        ]
        left_layout.addWidget(self._build_input_panel("商家参数", fields), 0)
        left_layout.addWidget(self._build_result_panel("计算结果", [
            ("毛利", "merchant_gross_profit", True),
            ("纯利", "merchant_profit_before_ad", True),
            ("预估 ROI", "merchant_gross_break_even_roi", True),
            ("保本 ROI", "merchant_break_even_roi", True),
            ("保本广告费", "merchant_max_ad_cost", True),
            ("目标ROI出价", "merchant_target_ad_cost", True),
            ("投放后利润", "merchant_profit_after_ad", False),
            ("当前 ROI", "merchant_actual_roi", False),
            ("有效成交额", "merchant_effective_revenue", False),
            ("非投放总成本", "merchant_hard_cost", False),
            ("平台扣点", "merchant_platform_fee", False),
            ("增值税", "merchant_vat_fee", False),
        ]), 0)
        layout.addWidget(left_pane, 5)
        pricing_panel = self._build_pricing_panel("出价策略参考", [
            ("千川全域目标 ROI", [
                ("ROI*0.5倍", "merchant_qc_roi_05"),
                ("ROI*0.6倍", "merchant_qc_roi_06"),
                ("ROI*0.7倍", "merchant_qc_roi_07"),
                ("ROI*0.8倍", "merchant_qc_roi_08"),
                ("ROI*0.9倍", "merchant_qc_roi_09"),
                ("ROI*1.0倍", "merchant_qc_roi_10"),
                ("ROI*1.1倍", "merchant_qc_roi_11"),
                ("ROI*1.2倍", "merchant_qc_roi_12"),
            ]),
            ("随心推手动出价", [
                ("0.8倍出价", "merchant_manual_bid_08"),
                ("0.9倍出价", "merchant_manual_bid_09"),
                ("1.0倍出价", "merchant_manual_bid_10"),
                ("1.1倍出价", "merchant_manual_bid_11"),
                ("1.2倍出价", "merchant_manual_bid_12"),
                ("1.3倍出价", "merchant_manual_bid_13"),
                ("1.4倍出价", "merchant_manual_bid_14"),
                ("1.5倍出价", "merchant_manual_bid_15"),
            ]),
        ], columns=2)
        layout.addWidget(pricing_panel, 3, Qt.AlignTop)
        return page

    def _build_creator_page(self):
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        left_pane = QWidget()
        left_layout = QVBoxLayout(left_pane)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)

        self.creator_price_spin = self._money_spin(99.0)
        self.creator_order_spin = self._ratio_spin(100.0, minimum=0, maximum=999999, decimals=0, step=10)
        self.creator_commission_rate_spin = self._percent_spin(20.0)
        self.creator_refund_rate_spin = self._percent_spin(5.0)
        self.creator_service_rate_spin = self._percent_spin(0.0)
        self.creator_tax_rate_spin = self._percent_spin(0.0)
        self.creator_ad_cost_spin = self._money_spin(300.0)
        self.creator_content_cost_spin = self._money_spin(0.0)

        fields = [
            ("商品售价", self.creator_price_spin, "商品成交单价，单位元"),
            ("成交件数", self.creator_order_spin, "预估可结算订单数"),
            ("佣金率", self.creator_commission_rate_spin, "百分比格式：20 = 20%"),
            ("退款率", self.creator_refund_rate_spin, "百分比格式：5 = 5%"),
            ("平台服务费", self.creator_service_rate_spin, "达人侧服务费，没有可填 0"),
            ("税费比例", self.creator_tax_rate_spin, "达人侧税费，没有可填 0"),
            ("投流成本", self.creator_ad_cost_spin, "达人为内容投放花的钱"),
            ("内容/样品成本", self.creator_content_cost_spin, "拍摄、剪辑、样品等成本"),
        ]
        left_layout.addWidget(self._build_input_panel("达人参数", fields), 0)
        left_layout.addWidget(self._build_result_panel("达人结果", [
            ("预估 GMV", "creator_gmv", True),
            ("结算 GMV", "creator_effective_gmv", True),
            ("佣金收入", "creator_commission", True),
            ("达人净利润", "creator_profit", True),
            ("达人 ROI", "creator_roi", True),
            ("保本 ROI", "creator_break_even_roi", True),
            ("保本投流成本", "creator_break_even_ad", True),
            ("可结算件数", "creator_effective_orders", False),
            ("平台服务费", "creator_service_fee", False),
            ("税费金额", "creator_tax_fee", False),
            ("总投入", "creator_total_cost", False),
            ("每单佣金", "creator_commission_per_order", False),
        ]), 0)
        layout.addWidget(left_pane, 5)
        pricing_panel = self._build_pricing_panel("出价策略参考", [
            ("千川全域目标 ROI", [
                ("ROI*0.5倍", "creator_qc_roi_05"),
                ("ROI*0.6倍", "creator_qc_roi_06"),
                ("ROI*0.7倍", "creator_qc_roi_07"),
                ("ROI*0.8倍", "creator_qc_roi_08"),
                ("ROI*0.9倍", "creator_qc_roi_09"),
                ("ROI*1.0倍", "creator_qc_roi_10"),
                ("ROI*1.1倍", "creator_qc_roi_11"),
                ("ROI*1.2倍", "creator_qc_roi_12"),
            ]),
            ("随心推手动出价", [
                ("0.8倍出价", "creator_manual_bid_08"),
                ("0.9倍出价", "creator_manual_bid_09"),
                ("1.0倍出价", "creator_manual_bid_10"),
                ("1.1倍出价", "creator_manual_bid_11"),
                ("1.2倍出价", "creator_manual_bid_12"),
                ("1.3倍出价", "creator_manual_bid_13"),
                ("1.4倍出价", "creator_manual_bid_14"),
                ("1.5倍出价", "creator_manual_bid_15"),
            ]),
        ], columns=2)
        layout.addWidget(pricing_panel, 3, Qt.AlignTop)
        return page

    def _build_input_panel(self, title, fields):
        group = QFrame()
        group.setObjectName("roiCard")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(14, 11, 14, 13)
        layout.setSpacing(8)
        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")
        layout.addWidget(title_label)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(7)
        for i, (label, widget, hint) in enumerate(fields):
            row, col = divmod(i, 2)
            grid.addWidget(self._make_field(label, widget, hint), row, col)
            widget.valueChanged.connect(self.recalculate)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        note = QLabel("百分比字段直接填数字，例如 5 表示 5%。鼠标滚轮不会改动输入值。")
        note.setObjectName("roiNote")
        note.setWordWrap(True)
        layout.addWidget(note)
        return group

    def _make_field(self, title, widget, hint):
        frame = QFrame()
        frame.setObjectName("roiField")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 7)
        layout.setSpacing(5)
        frame.setToolTip(hint)
        widget.setToolTip(hint)
        title_label = QLabel(title)
        title_label.setObjectName("roiFieldLabel")
        layout.addWidget(title_label)
        layout.addWidget(widget)
        return frame

    def _build_result_panel(self, panel_title, rows):
        group = QFrame()
        group.setObjectName("roiCard")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(14, 11, 14, 13)
        layout.setSpacing(8)
        title_label = QLabel(panel_title)
        title_label.setObjectName("cardTitle")
        layout.addWidget(title_label)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        grid.setAlignment(Qt.AlignTop)
        for i, (row_title, key, primary) in enumerate(rows):
            row, col = divmod(i, 4)
            grid.addWidget(self._build_metric_item(row_title, key, primary), row, col)
        for col in range(4):
            grid.setColumnStretch(col, 1)
        layout.addLayout(grid)

        status = QLabel("输入数据后自动判断利润空间。")
        status.setObjectName("roiStatus")
        status.setWordWrap(True)
        layout.addWidget(status)
        if "达人" in panel_title:
            self.creator_status_label = status
        else:
            self.merchant_status_label = status
        return group

    def _build_metric_item(self, row_title, key, primary=False):
        item = QFrame()
        item.setObjectName("roiMetricPrimary" if primary else "roiMetric")
        item_layout = QVBoxLayout(item)
        item_layout.setContentsMargins(9, 7, 9, 7)
        item_layout.setSpacing(3)
        title_label = QLabel(row_title)
        title_label.setObjectName("resultName")
        value_label = QLabel("--")
        value_label.setObjectName("roiPrimary" if primary else "roiValue")
        value_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        item_layout.addWidget(title_label)
        item_layout.addWidget(value_label)
        self._result_labels[key] = value_label
        return item

    def _build_pricing_panel(self, panel_title, sections, columns=4):
        group = QFrame()
        group.setObjectName("roiCard")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(14, 11, 14, 13)
        layout.setSpacing(8)
        title_label = QLabel(panel_title)
        title_label.setObjectName("cardTitle")
        layout.addWidget(title_label)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        row = 0
        for section_title, items in sections:
            section_label = QLabel(section_title)
            section_label.setObjectName("priceSectionTitle")
            grid.addWidget(section_label, row, 0, 1, columns)
            row += 1
            for i, (label, key) in enumerate(items):
                item_row = row + i // columns
                col = i % columns
                grid.addWidget(self._build_price_item(label, key), item_row, col)
            row += (len(items) + columns - 1) // columns
        for col in range(columns):
            grid.setColumnStretch(col, 1)
        layout.addLayout(grid)

        note = QLabel("参考网页口径：千川为保本 ROI 的倍率参考，随心推为单笔可承受出价倍率。")
        note.setObjectName("roiNote")
        note.setWordWrap(True)
        note.setMaximumHeight(44)
        layout.addWidget(note)
        return group

    def _build_price_item(self, label, key):
        item = QFrame()
        item.setObjectName("roiPriceItem")
        item_layout = QHBoxLayout(item)
        item_layout.setContentsMargins(8, 5, 8, 5)
        item_layout.setSpacing(6)
        title_label = QLabel(label)
        title_label.setObjectName("priceName")
        value_label = QLabel("--")
        value_label.setObjectName("roiPriceValue")
        value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        item_layout.addWidget(title_label)
        item_layout.addWidget(value_label, 1)
        self._result_labels[key] = value_label
        return item

    def _money_spin(self, default):
        return self._spin(default, 0, 999999999, 2, 1, prefix="¥ ")

    def _percent_spin(self, default):
        return self._spin(default, 0, 100, 2, 0.5, suffix=" %")

    def _ratio_spin(self, default, minimum=0.01, maximum=999999, decimals=2, step=0.1):
        return self._spin(default, minimum, maximum, decimals, step)

    def _spin(self, default, minimum, maximum, decimals, step, prefix="", suffix=""):
        spin = NoWheelDoubleSpinBox()
        spin.setRange(minimum, maximum)
        spin.setDecimals(decimals)
        spin.setSingleStep(step)
        spin.setPrefix(prefix)
        spin.setSuffix(suffix)
        spin.setValue(default)
        spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        spin.setMinimumHeight(30)
        spin.setMinimumWidth(104)
        spin.lineEdit().setAlignment(Qt.AlignRight)
        return spin

    def switch_mode(self, mode):
        self.mode = mode
        self.stack.setCurrentIndex(0 if mode == self.MODE_MERCHANT else 1)
        if mode == self.MODE_MERCHANT:
            self.subtitle_label.setText("商家版：按商品成本、退款、扣点和税费，计算保本 ROI 与出价档位。")
        else:
            self.subtitle_label.setText("达人版：不计算商品成本，只看佣金收入、投流成本和达人净利润。")
        self.recalculate()

    def recalculate(self):
        self._calculate_merchant()
        self._calculate_creator()
        self._last_metrics = self._merchant_metrics if self.mode == self.MODE_MERCHANT else self._creator_metrics

    def _calculate_merchant(self):
        price = self.price_spin.value()
        product_cost = self.product_cost_spin.value()
        freight_cost = self.freight_cost_spin.value()
        refund_rate = self.refund_rate_spin.value() / 100
        platform_rate = self.platform_rate_spin.value() / 100
        vat_rate = self.vat_rate_spin.value() / 100
        ad_cost = self.ad_cost_spin.value()
        target_roi = self.target_roi_spin.value()

        effective_revenue = price * max(0, 1 - refund_rate)
        platform_fee = effective_revenue * platform_rate
        vat_fee = effective_revenue * vat_rate
        hard_cost = product_cost + freight_cost + platform_fee + vat_fee
        gross_profit = price - product_cost - freight_cost
        profit_before_ad = effective_revenue - hard_cost
        max_ad_cost = max(0, profit_before_ad)
        gross_break_even_roi = price / gross_profit if gross_profit > 0 else None
        break_even_roi = effective_revenue / profit_before_ad if profit_before_ad > 0 else None
        actual_roi = effective_revenue / ad_cost if ad_cost > 0 else None
        profit_after_ad = profit_before_ad - ad_cost
        target_ad_cost = effective_revenue / target_roi if target_roi > 0 else 0
        target_profit = profit_before_ad - target_ad_cost
        cost_rate = hard_cost / effective_revenue * 100 if effective_revenue > 0 else 0
        profit_margin = profit_after_ad / effective_revenue * 100 if effective_revenue > 0 else 0

        self._set("merchant_gross_profit", self._fmy(gross_profit), gross_profit > 0)
        self._set("merchant_gross_break_even_roi", self._fra(gross_break_even_roi), gross_profit > 0)
        self._set("merchant_effective_revenue", self._fmy(effective_revenue))
        self._set("merchant_platform_fee", self._fmy(platform_fee))
        self._set("merchant_vat_fee", self._fmy(vat_fee))
        self._set("merchant_hard_cost", self._fmy(hard_cost))
        self._set("merchant_profit_before_ad", self._fmy(profit_before_ad), profit_before_ad > 0)
        self._set("merchant_profit_after_ad", self._fmy(profit_after_ad), profit_after_ad > 0)
        self._set("merchant_profit_margin", self._fpc(profit_margin), profit_after_ad > 0)
        self._set("merchant_max_ad_cost", self._fmy(max_ad_cost), profit_before_ad > 0)
        self._set("merchant_break_even_roi", self._fra(break_even_roi), profit_before_ad > 0)
        self._set("merchant_actual_roi", self._fra(actual_roi), actual_roi is not None and break_even_roi is not None and actual_roi >= break_even_roi)
        self._set("merchant_target_ad_cost", self._fmy(target_ad_cost), target_profit > 0)
        self._set("merchant_target_profit", self._fmy(target_profit), target_profit > 0)
        self._set("merchant_cost_rate", self._fpc(cost_rate), cost_rate < 100)

        for multiplier, key in [
            (0.5, "merchant_qc_roi_05"), (0.6, "merchant_qc_roi_06"),
            (0.7, "merchant_qc_roi_07"), (0.8, "merchant_qc_roi_08"),
            (0.9, "merchant_qc_roi_09"), (1.0, "merchant_qc_roi_10"),
            (1.1, "merchant_qc_roi_11"), (1.2, "merchant_qc_roi_12"),
        ]:
            value = break_even_roi * multiplier if break_even_roi is not None else None
            self._set(key, self._fra(value), value is not None)

        for multiplier, key in [
            (0.8, "merchant_manual_bid_08"), (0.9, "merchant_manual_bid_09"),
            (1.0, "merchant_manual_bid_10"), (1.1, "merchant_manual_bid_11"),
            (1.2, "merchant_manual_bid_12"), (1.3, "merchant_manual_bid_13"),
            (1.4, "merchant_manual_bid_14"), (1.5, "merchant_manual_bid_15"),
        ]:
            self._set(key, self._fmy(max_ad_cost * multiplier), profit_before_ad > 0)

        if price <= 0:
            self._status(self.merchant_status_label, "请输入售价。", "warn")
        elif profit_before_ad <= 0:
            self._status(self.merchant_status_label, "无利润空间，需调整售价、成本或退款率。", "bad")
        elif ad_cost > max_ad_cost:
            self._status(self.merchant_status_label, "广告花费已超过保本线，当前亏损。", "bad")
        elif actual_roi is not None and break_even_roi is not None and actual_roi < break_even_roi:
            self._status(self.merchant_status_label, "当前 ROI 低于保本 ROI。", "bad")
        else:
            self._status(self.merchant_status_label, "利润空间正常，重点看保本 ROI 和广告承受力。", "good")

        self._merchant_metrics = {
            "版本": "商家版",
            "售价": self._fmy(price),
            "产品成本": self._fmy(product_cost),
            "运费成本": self._fmy(freight_cost),
            "退款率": self._fpc(refund_rate * 100),
            "平台扣点": self._fpc(platform_rate * 100),
            "增值税": self._fpc(vat_rate * 100),
            "广告花费": self._fmy(ad_cost),
            "毛利": self._fmy(gross_profit),
            "有效成交额": self._fmy(effective_revenue),
            "投放前利润": self._fmy(profit_before_ad),
            "目标ROI出价": self._fmy(target_ad_cost),
            "保本ROI": self._fra(break_even_roi),
            "投放后利润": self._fmy(profit_after_ad),
        }

    def _calculate_creator(self):
        price = self.creator_price_spin.value()
        orders = self.creator_order_spin.value()
        commission_rate = self.creator_commission_rate_spin.value() / 100
        refund_rate = self.creator_refund_rate_spin.value() / 100
        service_rate = self.creator_service_rate_spin.value() / 100
        tax_rate = self.creator_tax_rate_spin.value() / 100
        ad_cost = self.creator_ad_cost_spin.value()
        content_cost = self.creator_content_cost_spin.value()

        gmv = price * orders
        effective_orders = orders * max(0, 1 - refund_rate)
        effective_gmv = price * effective_orders
        commission = effective_gmv * commission_rate
        service_fee = commission * service_rate
        tax_fee = commission * tax_rate
        net_commission = commission - service_fee - tax_fee
        total_cost = ad_cost + content_cost + service_fee + tax_fee
        profit = commission - total_cost
        roi = commission / total_cost if total_cost > 0 else None
        break_even_ad = max(0, commission - content_cost - service_fee - tax_fee)
        commission_per_order = commission / effective_orders if effective_orders > 0 else 0
        net_commission_per_order = net_commission / effective_orders if effective_orders > 0 else 0
        break_even_roi = effective_gmv / net_commission if net_commission > 0 else None
        profit_margin = profit / commission * 100 if commission > 0 else 0

        self._set("creator_gmv", self._fmy(gmv))
        self._set("creator_effective_gmv", self._fmy(effective_gmv))
        self._set("creator_effective_orders", f"{effective_orders:.0f}")
        self._set("creator_commission", self._fmy(commission), commission > 0)
        self._set("creator_profit", self._fmy(profit), profit > 0)
        self._set("creator_roi", self._fra(roi), profit >= 0 if roi is not None else None)
        self._set("creator_break_even_roi", self._fra(break_even_roi), break_even_roi is not None)
        self._set("creator_break_even_ad", self._fmy(break_even_ad), commission > 0)
        self._set("creator_service_fee", self._fmy(service_fee))
        self._set("creator_tax_fee", self._fmy(tax_fee))
        self._set("creator_total_cost", self._fmy(total_cost), profit >= 0)
        self._set("creator_commission_per_order", self._fmy(commission_per_order))
        self._set("creator_profit_margin", self._fpc(profit_margin), profit > 0)

        for multiplier, key in [
            (0.5, "creator_qc_roi_05"), (0.6, "creator_qc_roi_06"),
            (0.7, "creator_qc_roi_07"), (0.8, "creator_qc_roi_08"),
            (0.9, "creator_qc_roi_09"), (1.0, "creator_qc_roi_10"),
            (1.1, "creator_qc_roi_11"), (1.2, "creator_qc_roi_12"),
        ]:
            value = break_even_roi * multiplier if break_even_roi is not None else None
            self._set(key, self._fra(value), value is not None)

        for multiplier, key in [
            (0.8, "creator_manual_bid_08"), (0.9, "creator_manual_bid_09"),
            (1.0, "creator_manual_bid_10"), (1.1, "creator_manual_bid_11"),
            (1.2, "creator_manual_bid_12"), (1.3, "creator_manual_bid_13"),
            (1.4, "creator_manual_bid_14"), (1.5, "creator_manual_bid_15"),
        ]:
            self._set(key, self._fmy(net_commission_per_order * multiplier), net_commission > 0)

        if price <= 0 or orders <= 0:
            self._status(self.creator_status_label, "请输入商品售价和成交件数。", "warn")
        elif commission <= 0:
            self._status(self.creator_status_label, "佣金收入为 0，需检查佣金率或退款率。", "bad")
        elif profit < 0:
            self._status(self.creator_status_label, "达人净利润为负，投流或内容成本偏高。", "bad")
        else:
            self._status(self.creator_status_label, "达人净利润为正，可继续评估内容放量。", "good")

        self._creator_metrics = {
            "版本": "达人版",
            "商品售价": self._fmy(price),
            "成交件数": f"{orders:.0f}",
            "佣金率": self._fpc(commission_rate * 100),
            "退款率": self._fpc(refund_rate * 100),
            "投流成本": self._fmy(ad_cost),
            "内容/样品成本": self._fmy(content_cost),
            "结算GMV": self._fmy(effective_gmv),
            "佣金收入": self._fmy(commission),
            "达人ROI": self._fra(roi),
            "保本ROI": self._fra(break_even_roi),
            "达人净利润": self._fmy(profit),
        }

    def reset_defaults(self):
        if self.mode == self.MODE_MERCHANT:
            pairs = [
                (self.price_spin, 99), (self.product_cost_spin, 30), (self.freight_cost_spin, 5),
                (self.refund_rate_spin, 5), (self.platform_rate_spin, 5), (self.vat_rate_spin, 13),
                (self.ad_cost_spin, 0), (self.target_roi_spin, 2.5),
            ]
        else:
            pairs = [
                (self.creator_price_spin, 99), (self.creator_order_spin, 100), (self.creator_commission_rate_spin, 20),
                (self.creator_refund_rate_spin, 5), (self.creator_service_rate_spin, 0), (self.creator_tax_rate_spin, 0),
                (self.creator_ad_cost_spin, 300), (self.creator_content_cost_spin, 0),
            ]
        for widget, value in pairs:
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
        self.recalculate()

    def copy_results(self):
        if not self._last_metrics:
            self.recalculate()
        QApplication.clipboard().setText(
            "ROI 计算器结果\n" + "\n".join(f"{key}：{value}" for key, value in self._last_metrics.items())
        )

    def _set(self, key, text, good=None):
        label = self._result_labels.get(key)
        if not label:
            return
        color = "#86EFAC" if good is True else ("#FCA5A5" if good is False else "#F8FAFC")
        label.setText(text)
        label.setStyleSheet(f"color:{color};font-weight:800;font-size:13px;background:transparent;border:none;")

    def _status(self, label, text, level):
        colors = {
            "good": ("#052E16", "#22C55E", "#BBF7D0"),
            "warn": ("#2A1F0B", "#F59E0B", "#FDE68A"),
            "bad": ("#3A1012", "#EF4444", "#FCA5A5"),
        }
        bg, border, fg = colors.get(level, colors["warn"])
        label.setText(text)
        label.setStyleSheet(
            f"background:{bg};border:1px solid {border};border-radius:6px;"
            f"color:{fg};padding:8px 10px;font-weight:700;"
        )

    def _fmy(self, value):
        return f"¥{value:,.2f}"

    def _fpc(self, value):
        return f"{value:.2f}%"

    def _fra(self, value):
        return f"{value:.2f}" if value is not None else "--"

    def _apply_styles(self):
        self.setStyleSheet("""
            QWidget#roiCalculatorPage {
                background: #0B1120;
            }
            QFrame#roiShell {
                background: #111827;
                border: 1px solid #334155;
                border-radius: 8px;
            }
            QLabel#pageTitle {
                color: #F8FAFC;
                font-size: 18px;
                font-weight: 800;
                background: transparent;
                border: none;
            }
            QLabel#mutedText {
                color: #CBD5E1;
                font-size: 12px;
                background: transparent;
                border: none;
            }
            QPushButton#modeButton {
                background: #0B1220;
                border: 1px solid #475569;
                border-radius: 6px;
                color: #CBD5E1;
                padding: 7px 12px;
                font-weight: 700;
            }
            QPushButton#modeButton:checked {
                background: #2563EB;
                border-color: #60A5FA;
                color: #FFFFFF;
            }
            QFrame#roiCard {
                background: #0F172A;
                border: 1px solid #475569;
                border-radius: 8px;
            }
            QLabel#cardTitle {
                color: #F8FAFC;
                font-size: 14px;
                font-weight: 800;
                background: transparent;
                border: none;
            }
            QFrame#roiField {
                background: #111827;
                border: 1px solid #475569;
                border-radius: 6px;
            }
            QFrame#roiMetric {
                background: #111827;
                border: 1px solid #334155;
                border-radius: 7px;
            }
            QFrame#roiMetricPrimary {
                background: #0B2A3A;
                border: 1px solid #0EA5E9;
                border-radius: 7px;
            }
            QFrame#roiPriceItem {
                background: #111827;
                border: 1px solid #334155;
                border-radius: 6px;
            }
            QLabel#roiFieldLabel,
            QLabel#resultName,
            QLabel#priceName {
                color: #E2E8F0;
                font-size: 12px;
                font-weight: 700;
                background: transparent;
                border: none;
            }
            QLabel#roiFieldHint {
                color: #94A3B8;
                font-size: 10px;
                background: transparent;
                border: none;
            }
            QLabel#priceSectionTitle {
                color: #93C5FD;
                font-size: 12px;
                font-weight: 800;
                background: transparent;
                border: none;
                padding-top: 3px;
            }
            QLabel#roiNote {
                color: #94A3B8;
                font-size: 11px;
                background: #0B1220;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 7px 9px;
            }
            QLabel#roiStatus {
                background: #0B1220;
                border: 1px solid #334155;
                border-radius: 6px;
                color: #CBD5E1;
                padding: 8px 10px;
                font-weight: 700;
            }
            QLabel#roiValue,
            QLabel#roiPrimary,
            QLabel#roiPriceValue {
                background: transparent;
                border: none;
            }
            QDoubleSpinBox {
                background: #020617;
                border: 1px solid #64748B;
                border-radius: 6px;
                color: #F8FAFC;
                padding: 5px 8px;
                font-weight: 800;
                selection-background-color: #2563EB;
            }
            QDoubleSpinBox:focus {
                border-color: #60A5FA;
            }
            QPushButton {
                background: #1E293B;
                border: 1px solid #475569;
                border-radius: 6px;
                color: #E2E8F0;
                padding: 7px 14px;
                font-weight: 700;
            }
            QPushButton:hover {
                background: #334155;
                border-color: #60A5FA;
            }
        """)
