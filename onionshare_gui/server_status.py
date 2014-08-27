from PyQt4 import QtCore, QtGui

import common
from onionshare import strings, helpers

class ServerStatus(QtGui.QVBoxLayout):
    def __init__(self):
        super(ServerStatus, self).__init__()
        self.addSpacing(10)

        # server layout
        self.status_image = QtGui.QImage('{0}/server_stopped.png'.format(common.onionshare_gui_dir))
        status_image_label = QtGui.QLabel()
        status_image_label.setPixmap(QtGui.QPixmap.fromImage(self.status_image))
        status_image_label.setFixedWidth(30)
        self.start_server_button = QtGui.QPushButton(strings._('gui_start_server'))
        self.start_server_button.clicked.connect(self.start_server)
        self.stop_server_button = QtGui.QPushButton(strings._('gui_stop_server'))
        self.stop_server_button.clicked.connect(self.stop_server)
        server_layout = QtGui.QHBoxLayout()
        server_layout.addWidget(status_image_label)
        server_layout.addWidget(self.start_server_button)
        server_layout.addWidget(self.stop_server_button)

        # url layout
        url_font = QtGui.QFont()
        url_font.setPointSize(8)
        self.url_label = QtGui.QLabel('http://mry2aqolyzxwfxpt.onion/ x6justoparr5ayreqj6zyf6w2e')
        self.url_label.setFont(url_font)
        self.url_label.setWordWrap(True)
        self.url_label.setAlignment(QtCore.Qt.AlignCenter)
        self.url_label.setMargin(3)
        self.copy_url_button = QtGui.QPushButton(strings._('gui_copy_url'))
        self.copy_url_button.clicked.connect(self.copy_url)
        url_layout = QtGui.QHBoxLayout()
        url_layout.addWidget(self.url_label)
        url_layout.addWidget(self.copy_url_button)
        # url fields start hidden, until there's a URL
        self.url_label.hide()
        self.copy_url_button.hide()

        # add the widgets
        self.addLayout(server_layout)
        self.addLayout(url_layout)

    def start_server(self):
        pass

    def stop_server(self):
        pass

    def copy_url(self):
        pass

