# -*- coding: utf-8 -*-

################################################################################
## Form generated from reading UI file 'pipeline_applet_qt_template.ui'
##
## Created by: Qt User Interface Compiler version 6.11.2
##
## WARNING! All changes made in this file will be lost when recompiling UI file!
################################################################################

from PySide6.QtCore import (QCoreApplication, QDate, QDateTime, QLocale,
    QMetaObject, QObject, QPoint, QRect,
    QSize, QTime, QUrl, Qt)
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QCursor,
    QFont, QFontDatabase, QGradient, QIcon,
    QImage, QKeySequence, QLinearGradient, QPainter,
    QPalette, QPixmap, QRadialGradient, QTransform)
from PySide6.QtWidgets import (QApplication, QComboBox, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLayout, QMainWindow,
    QMenuBar, QPlainTextEdit, QPushButton, QSizePolicy,
    QSpacerItem, QStackedWidget, QStatusBar, QTabWidget,
    QVBoxLayout, QWidget)

class Ui_PipelineAppletWindow(object):
    def setupUi(self, PipelineAppletWindow):
        if not PipelineAppletWindow.objectName():
            PipelineAppletWindow.setObjectName(u"PipelineAppletWindow")
        PipelineAppletWindow.resize(1270, 950)
        PipelineAppletWindow.setMinimumSize(QSize(1270, 950))
        self.centralwidget = QWidget(PipelineAppletWindow)
        self.centralwidget.setObjectName(u"centralwidget")
        self.mainVerticalLayout = QVBoxLayout(self.centralwidget)
        self.mainVerticalLayout.setObjectName(u"mainVerticalLayout")
        self.gridLayout = QGridLayout()
        self.gridLayout.setObjectName(u"gridLayout")
        self.gridLayout.setSizeConstraint(QLayout.SizeConstraint.SetDefaultConstraint)
        self.gridLayout.setHorizontalSpacing(1)
        self.gridLayout.setVerticalSpacing(5)
        self.gridLayout.setContentsMargins(1, 1, 1, 1)
        self.newProjectButton = QPushButton(self.centralwidget)
        self.newProjectButton.setObjectName(u"newProjectButton")
        sizePolicy = QSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        sizePolicy.setHorizontalStretch(0)
        sizePolicy.setVerticalStretch(0)
        sizePolicy.setHeightForWidth(self.newProjectButton.sizePolicy().hasHeightForWidth())
        self.newProjectButton.setSizePolicy(sizePolicy)
        self.newProjectButton.setMinimumSize(QSize(96, 26))
        self.newProjectButton.setLayoutDirection(Qt.LayoutDirection.LeftToRight)

        self.gridLayout.addWidget(self.newProjectButton, 0, 3, 1, 1)

        self.openProjectButton = QPushButton(self.centralwidget)
        self.openProjectButton.setObjectName(u"openProjectButton")
        sizePolicy.setHeightForWidth(self.openProjectButton.sizePolicy().hasHeightForWidth())
        self.openProjectButton.setSizePolicy(sizePolicy)
        self.openProjectButton.setMinimumSize(QSize(96, 26))

        self.gridLayout.addWidget(self.openProjectButton, 1, 3, 1, 1)

        self.AboutButton = QPushButton(self.centralwidget)
        self.AboutButton.setObjectName(u"AboutButton")
        sizePolicy.setHeightForWidth(self.AboutButton.sizePolicy().hasHeightForWidth())
        self.AboutButton.setSizePolicy(sizePolicy)
        self.AboutButton.setMinimumSize(QSize(96, 26))

        self.gridLayout.addWidget(self.AboutButton, 0, 0, 1, 1)

        self.titleLabel = QLabel(self.centralwidget)
        self.titleLabel.setObjectName(u"titleLabel")
        self.titleLabel.setEnabled(True)
        sizePolicy.setHeightForWidth(self.titleLabel.sizePolicy().hasHeightForWidth())
        self.titleLabel.setSizePolicy(sizePolicy)
        self.titleLabel.setMaximumSize(QSize(500, 16777215))
        self.titleLabel.setStyleSheet(u"font-size: 32px; font-weight: bold;")

        self.gridLayout.addWidget(self.titleLabel, 1, 0, 1, 1)

        self.horizontalSpacer = QSpacerItem(40, 20, QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        self.gridLayout.addItem(self.horizontalSpacer, 1, 1, 1, 1)


        self.mainVerticalLayout.addLayout(self.gridLayout)

        self.topLayout = QHBoxLayout()
        self.topLayout.setObjectName(u"topLayout")
        self.stageDisplayFrame = QFrame(self.centralwidget)
        self.stageDisplayFrame.setObjectName(u"stageDisplayFrame")
        self.stageDisplayFrame.setFrameShape(QFrame.Shape.Box)
        self.stageDisplayLayout = QVBoxLayout(self.stageDisplayFrame)
        self.stageDisplayLayout.setObjectName(u"stageDisplayLayout")
        self.stageTitleLabel = QLabel(self.stageDisplayFrame)
        self.stageTitleLabel.setObjectName(u"stageTitleLabel")
        self.stageTitleLabel.setStyleSheet(u"font-size: 16px; font-weight: bold;")
        self.stageTitleLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.stageDisplayLayout.addWidget(self.stageTitleLabel)

        self.stageStack = QStackedWidget(self.stageDisplayFrame)
        self.stageStack.setObjectName(u"stageStack")
        self.welcomePage = QWidget()
        self.welcomePage.setObjectName(u"welcomePage")
        self.welcomePageLayout = QVBoxLayout(self.welcomePage)
        self.welcomePageLayout.setObjectName(u"welcomePageLayout")
        self.welcomeLabel = QLabel(self.welcomePage)
        self.welcomeLabel.setObjectName(u"welcomeLabel")
        self.welcomeLabel.setAutoFillBackground(False)
        self.welcomeLabel.setFrameShape(QFrame.Shape.NoFrame)
        self.welcomeLabel.setFrameShadow(QFrame.Shadow.Plain)
        self.welcomeLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.welcomePageLayout.addWidget(self.welcomeLabel)

        self.stageStack.addWidget(self.welcomePage)

        self.stageDisplayLayout.addWidget(self.stageStack)


        self.topLayout.addWidget(self.stageDisplayFrame)

        self.controlLayout = QVBoxLayout()
        self.controlLayout.setObjectName(u"controlLayout")
        self.pipelineLayout = QVBoxLayout()
        self.pipelineLayout.setObjectName(u"pipelineLayout")
        self.panelLabel = QLabel(self.centralwidget)
        self.panelLabel.setObjectName(u"panelLabel")
        self.panelLabel.setStyleSheet(u"font-weight: bold;")
        self.panelLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.pipelineLayout.addWidget(self.panelLabel)

        self.statusLabel = QLabel(self.centralwidget)
        self.statusLabel.setObjectName(u"statusLabel")
        self.statusLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.statusLabel.setWordWrap(True)

        self.pipelineLayout.addWidget(self.statusLabel)

        self.horizontalLayout = QHBoxLayout()
        self.horizontalLayout.setObjectName(u"horizontalLayout")
        self.horizontalLayout.setContentsMargins(1, 1, 1, 1)
        self.newScanButton = QPushButton(self.centralwidget)
        self.newScanButton.setObjectName(u"newScanButton")

        self.horizontalLayout.addWidget(self.newScanButton)

        self.newDiffButton = QPushButton(self.centralwidget)
        self.newDiffButton.setObjectName(u"newDiffButton")

        self.horizontalLayout.addWidget(self.newDiffButton)


        self.pipelineLayout.addLayout(self.horizontalLayout)

        self.sourcePipelineLabel = QLabel(self.centralwidget)
        self.sourcePipelineLabel.setObjectName(u"sourcePipelineLabel")

        self.pipelineLayout.addWidget(self.sourcePipelineLabel)

        self.sourceCombo = QComboBox(self.centralwidget)
        self.sourceCombo.addItem("")
        self.sourceCombo.setObjectName(u"sourceCombo")

        self.pipelineLayout.addWidget(self.sourceCombo)

        self.diffPipelineLabel = QLabel(self.centralwidget)
        self.diffPipelineLabel.setObjectName(u"diffPipelineLabel")

        self.pipelineLayout.addWidget(self.diffPipelineLabel)

        self.diffCombo = QComboBox(self.centralwidget)
        self.diffCombo.addItem("")
        self.diffCombo.setObjectName(u"diffCombo")

        self.pipelineLayout.addWidget(self.diffCombo)


        self.controlLayout.addLayout(self.pipelineLayout)

        self.line_2 = QFrame(self.centralwidget)
        self.line_2.setObjectName(u"line_2")
        self.line_2.setFrameShape(QFrame.Shape.HLine)
        self.line_2.setFrameShadow(QFrame.Shadow.Sunken)

        self.controlLayout.addWidget(self.line_2)

        self.perscan = QLabel(self.centralwidget)
        self.perscan.setObjectName(u"perscan")
        self.perscan.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.controlLayout.addWidget(self.perscan)

        self.stage1Button = QPushButton(self.centralwidget)
        self.stage1Button.setObjectName(u"stage1Button")

        self.controlLayout.addWidget(self.stage1Button)

        self.stage2Button = QPushButton(self.centralwidget)
        self.stage2Button.setObjectName(u"stage2Button")

        self.controlLayout.addWidget(self.stage2Button)

        self.stage3Button = QPushButton(self.centralwidget)
        self.stage3Button.setObjectName(u"stage3Button")

        self.controlLayout.addWidget(self.stage3Button)

        self.stage4Button = QPushButton(self.centralwidget)
        self.stage4Button.setObjectName(u"stage4Button")

        self.controlLayout.addWidget(self.stage4Button)

        self.line = QFrame(self.centralwidget)
        self.line.setObjectName(u"line")
        self.line.setFrameShape(QFrame.Shape.HLine)
        self.line.setFrameShadow(QFrame.Shadow.Sunken)

        self.controlLayout.addWidget(self.line)

        self.forcomp = QLabel(self.centralwidget)
        self.forcomp.setObjectName(u"forcomp")
        self.forcomp.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.controlLayout.addWidget(self.forcomp)

        self.stage5Button = QPushButton(self.centralwidget)
        self.stage5Button.setObjectName(u"stage5Button")

        self.controlLayout.addWidget(self.stage5Button)

        self.stage6Button = QPushButton(self.centralwidget)
        self.stage6Button.setObjectName(u"stage6Button")

        self.controlLayout.addWidget(self.stage6Button)

        self.stage7Button = QPushButton(self.centralwidget)
        self.stage7Button.setObjectName(u"stage7Button")

        self.controlLayout.addWidget(self.stage7Button)

        self.stage8Button = QPushButton(self.centralwidget)
        self.stage8Button.setObjectName(u"stage8Button")

        self.controlLayout.addWidget(self.stage8Button)

        self.controlSpacer = QSpacerItem(20, 40, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)

        self.controlLayout.addItem(self.controlSpacer)


        self.topLayout.addLayout(self.controlLayout)

        self.topLayout.setStretch(0, 3)
        self.topLayout.setStretch(1, 1)

        self.mainVerticalLayout.addLayout(self.topLayout)

        self.outputTabs = QTabWidget(self.centralwidget)
        self.outputTabs.setObjectName(u"outputTabs")
        self.logTab = QWidget()
        self.logTab.setObjectName(u"logTab")
        self.logTabLayout = QVBoxLayout(self.logTab)
        self.logTabLayout.setObjectName(u"logTabLayout")
        self.logTabLayout.setContentsMargins(0, 0, 0, 0)
        self.appLogConsole = QPlainTextEdit(self.logTab)
        self.appLogConsole.setObjectName(u"appLogConsole")
        self.appLogConsole.setStyleSheet(u"border-radius: 5px; background-color: grey;")
        self.appLogConsole.setReadOnly(True)

        self.logTabLayout.addWidget(self.appLogConsole)

        self.outputTabs.addTab(self.logTab, "")
        self.terminalTab = QWidget()
        self.terminalTab.setObjectName(u"terminalTab")
        self.terminalTabLayout = QVBoxLayout(self.terminalTab)
        self.terminalTabLayout.setObjectName(u"terminalTabLayout")
        self.terminalTabLayout.setContentsMargins(0, 0, 0, 0)
        self.terminalConsole = QPlainTextEdit(self.terminalTab)
        self.terminalConsole.setObjectName(u"terminalConsole")
        self.terminalConsole.setStyleSheet(u"border-radius: 5px; background-color: grey;")
        self.terminalConsole.setReadOnly(True)

        self.terminalTabLayout.addWidget(self.terminalConsole)

        self.outputTabs.addTab(self.terminalTab, "")

        self.mainVerticalLayout.addWidget(self.outputTabs)

        self.runStatusLabel = QLabel(self.centralwidget)
        self.runStatusLabel.setObjectName(u"runStatusLabel")
        self.runStatusLabel.setStyleSheet(u"color: #666666; font-weight: bold; padding: 4px;")
        self.runStatusLabel.setAlignment(Qt.AlignmentFlag.AlignLeading|Qt.AlignmentFlag.AlignLeft|Qt.AlignmentFlag.AlignVCenter)

        self.mainVerticalLayout.addWidget(self.runStatusLabel)

        PipelineAppletWindow.setCentralWidget(self.centralwidget)
        self.menubar = QMenuBar(PipelineAppletWindow)
        self.menubar.setObjectName(u"menubar")
        self.menubar.setGeometry(QRect(0, 0, 1270, 33))
        PipelineAppletWindow.setMenuBar(self.menubar)
        self.statusbar = QStatusBar(PipelineAppletWindow)
        self.statusbar.setObjectName(u"statusbar")
        PipelineAppletWindow.setStatusBar(self.statusbar)

        self.retranslateUi(PipelineAppletWindow)

        self.stageStack.setCurrentIndex(0)
        self.outputTabs.setCurrentIndex(0)


        QMetaObject.connectSlotsByName(PipelineAppletWindow)
    # setupUi

    def retranslateUi(self, PipelineAppletWindow):
        PipelineAppletWindow.setWindowTitle(QCoreApplication.translate("PipelineAppletWindow", u"SLAM Pipeline Applet", None))
        self.newProjectButton.setText(QCoreApplication.translate("PipelineAppletWindow", u"New Project...", None))
        self.openProjectButton.setText(QCoreApplication.translate("PipelineAppletWindow", u"Open Project...", None))
        self.AboutButton.setText(QCoreApplication.translate("PipelineAppletWindow", u"About", None))
        self.titleLabel.setText(QCoreApplication.translate("PipelineAppletWindow", u"D.E.L.T.A Processing Pipeline", None))
        self.stageTitleLabel.setText(QCoreApplication.translate("PipelineAppletWindow", u"No stage selected", None))
        self.welcomeLabel.setText(QCoreApplication.translate("PipelineAppletWindow", u"Select a stage from the right to begin.", None))
        self.panelLabel.setText(QCoreApplication.translate("PipelineAppletWindow", u"PROJECT STATUS", None))
        self.statusLabel.setText(QCoreApplication.translate("PipelineAppletWindow", u"No active project", None))
        self.newScanButton.setText(QCoreApplication.translate("PipelineAppletWindow", u"New Scan", None))
        self.newDiffButton.setText(QCoreApplication.translate("PipelineAppletWindow", u"New Diff", None))
        self.sourcePipelineLabel.setText(QCoreApplication.translate("PipelineAppletWindow", u"Source pipeline:", None))
        self.sourceCombo.setItemText(0, QCoreApplication.translate("PipelineAppletWindow", u"(none)", None))

        self.diffPipelineLabel.setText(QCoreApplication.translate("PipelineAppletWindow", u"Diff pipeline:", None))
        self.diffCombo.setItemText(0, QCoreApplication.translate("PipelineAppletWindow", u"(none)", None))

        self.perscan.setText(QCoreApplication.translate("PipelineAppletWindow", u"Per Scan", None))
        self.stage1Button.setText(QCoreApplication.translate("PipelineAppletWindow", u"Stage 1: SLAM", None))
        self.stage2Button.setText(QCoreApplication.translate("PipelineAppletWindow", u"Stage 2: Level", None))
        self.stage3Button.setText(QCoreApplication.translate("PipelineAppletWindow", u"Stage 3: Cleanup", None))
        self.stage4Button.setText(QCoreApplication.translate("PipelineAppletWindow", u"Stage 4: Segment", None))
        self.forcomp.setText(QCoreApplication.translate("PipelineAppletWindow", u"For Comparison", None))
        self.stage5Button.setText(QCoreApplication.translate("PipelineAppletWindow", u"Stage 5: Diff", None))
        self.stage6Button.setText(QCoreApplication.translate("PipelineAppletWindow", u"Stage 6: Classify", None))
        self.stage7Button.setText(QCoreApplication.translate("PipelineAppletWindow", u"Stage 7: Surface", None))
        self.stage8Button.setText(QCoreApplication.translate("PipelineAppletWindow", u"Stage 8: Export", None))
        self.appLogConsole.setPlainText("")
        self.outputTabs.setTabText(self.outputTabs.indexOf(self.logTab), QCoreApplication.translate("PipelineAppletWindow", u"Log", None))
        self.terminalConsole.setPlainText(QCoreApplication.translate("PipelineAppletWindow", u"No subprocess output yet.", None))
        self.outputTabs.setTabText(self.outputTabs.indexOf(self.terminalTab), QCoreApplication.translate("PipelineAppletWindow", u"Terminal", None))
        self.runStatusLabel.setText(QCoreApplication.translate("PipelineAppletWindow", u"Idle", None))
    # retranslateUi

