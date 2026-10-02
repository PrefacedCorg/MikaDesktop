from PySide6.QtWidgets import QLabel
import uuid

class LabelElement(QLabel):
    def __init__(self, parent=None, text:str=""):
        super().__init__(parent)
        self.setText(text) 
        self.uuid = str(uuid.uuid4())

    def getUUID(self):
        return self.uuid
    
class ElementList(list):
    """元素列表。

    只保留本项目实际用到的接口：``addElement`` 以及 list 自身的迭代与下标访问
    （``XHTWindow.setElementList`` 就是靠迭代来挂载元素的）。原来还有
    ``getElements`` / ``setElement`` / ``delElement``，全项目无任何调用点。
    """

    def addElement(self, element:LabelElement):
        self.append(element)