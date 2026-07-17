import logging
import os

import vtk

import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin


#
# ICERecon
#

class ICERecon(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("ICE Reconstruction")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "Ultrasound")]
        self.parent.dependencies = []
        self.parent.contributors = ["Junichi Tokuda (Brigham and Women's Hospital)"]
        self.parent.helpText = _("""
Reconstructs a 3D model of the heart chamber from a series of 2D intracardiac
echo (ICE) images. Not yet implemented.
""")
        self.parent.acknowledgementText = _("""
This file was originally developed as part of the ICERecon extension.
""")


#
# ICEReconWidget
#

class ICEReconWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """Uses ScriptedLoadableModuleWidget base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)
        self.logic = None

    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)

        uiWidget = slicer.util.loadUI(self.resourcePath("UI/ICERecon.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        uiWidget.setMRMLScene(slicer.mrmlScene)

        self.logic = ICEReconLogic()

    def cleanup(self):
        self.removeObservers()


#
# ICEReconLogic
#

class ICEReconLogic(ScriptedLoadableModuleLogic):
    """This class implements all the actual computation done by the module.
    """

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)

    def process(self):
        # TODO: implement 3D reconstruction from 2D ICE images.
        raise NotImplementedError("ICEReconLogic.process() is not implemented yet.")


#
# ICEReconTest
#

class ICEReconTest(ScriptedLoadableModuleTest):
    """
    This is the test case for your scripted module.
    """

    def setUp(self):
        slicer.mrmlScene.Clear()

    def runTest(self):
        self.setUp()
        self.test_ICERecon1()

    def test_ICERecon1(self):
        self.delayDisplay("No automated tests implemented yet")
        self.delayDisplay("Test passed")
