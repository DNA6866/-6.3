from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ui_components import FileDropArea


class FileTabsMixin:
    def build_card_page(self, title, content_widget):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("appTitle")
        outer.addWidget(heading)
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 16)
        card_layout.addWidget(content_widget)
        outer.addWidget(card, stretch=1)
        return page

    def build_file_tab(self, name):
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.setSpacing(12)

        if name != '保存视频':
            drop_area = FileDropArea(f"拖拽文件到这里，或点击选择【{name}】文件")
            drop_area.filesDropped.connect(lambda files, t=name: self.handle_files_dropped(t, files))
            drop_area.clicked.connect(lambda t=name: self.choose_import_files_to_tab(t))
            tab_layout.addWidget(drop_area)

        btn_layout = QHBoxLayout()
        select_all_chk = QCheckBox("全选")
        select_all_chk.stateChanged.connect(lambda state, t=name: self.toggle_select_all(t, state))
        self.select_all_cbx[name] = select_all_chk

        open_dir_explorer_btn = QPushButton("打开所在目录")
        open_dir_explorer_btn.clicked.connect(lambda checked, t=name: self.open_directory_explorer(t))
        refresh_btn = QPushButton("刷新列表")
        refresh_btn.clicked.connect(lambda checked, t=name: self.refresh_directory_async(t))
        count_label = QLabel("已选 0 / 共 0 个素材（含子目录）")
        count_label.setObjectName("mutedText")
        count_label.setAlignment(Qt.AlignVCenter | Qt.AlignRight)
        self.asset_count_labels[name] = count_label

        btn_layout.addWidget(select_all_chk)
        btn_layout.addWidget(open_dir_explorer_btn)
        btn_layout.addWidget(refresh_btn)
        # 素材区标签页：显示"更换产品目录"按钮
        if name in [
            '音频素材', '封面图片', 'AI开场视频', '钩子素材',
            '第一轨道', '第二段指定素材', '视频素材', '背景音乐'
        ]:
            change_dir_btn = QPushButton("更换产品目录")
            change_dir_btn.setMaximumWidth(150)
            change_dir_btn.clicked.connect(lambda checked, t=name: self.change_product_directory(t))
            btn_layout.addWidget(change_dir_btn)
        btn_layout.addWidget(count_label, stretch=1)

        if name == '保存视频':
            save_btn = QPushButton("保存全局配置")
            save_btn.clicked.connect(lambda: self.save_configuration(False))
            open_explorer_btn = QPushButton("打开输出文件夹")
            open_explorer_btn.clicked.connect(self.open_output_folder)
            btn_layout.addWidget(save_btn)
            btn_layout.addWidget(open_explorer_btn)

        tab_layout.addLayout(btn_layout)

        if name in ['音频素材']:
            table = QTableWidget(0, 4)
            table.setHorizontalHeaderLabels(['选择', '素材名称', '文件路径', '当前状态'])
        else:
            table = QTableWidget(0, 3)
            table.setHorizontalHeaderLabels(['选择', '素材名称', '文件路径'])

        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        table.setColumnWidth(0, 68)
        table.itemChanged.connect(lambda item, t=name: self._on_asset_table_item_changed(t, item))
        table.itemClicked.connect(lambda item, t=name: self._on_asset_table_item_clicked(t, item))
        self.tables[name] = table
        tab_layout.addWidget(table)
        return tab

    def build_file_page(self, name):
        tab = self.build_file_tab(name)
        return self.build_card_page(name, tab)

