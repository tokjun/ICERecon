import logging
import os
import time
from typing import Annotated

import numpy as np
import vtk
import vtk.util.numpy_support as vtk_np

import slicer
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin
from slicer.parameterNodeWrapper import (
    parameterNodeWrapper,
    WithinRange,
)

from slicer import vtkMRMLScalarVolumeNode
from slicer import vtkMRMLSegmentationNode
from slicer import vtkMRMLLinearTransformNode


#
# ICESim
#

class ICESim(ScriptedLoadableModule):
    """Uses ScriptedLoadableModule base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("ICE Simulator")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "Ultrasound")]
        self.parent.dependencies = []
        self.parent.contributors = ["Junichi Tokuda (Brigham and Women's Hospital)"]
        self.parent.helpText = _("""
Simulates a 2D intracardiac echo (ICE) image from a 3D segmentation of the
heart chambers and vessels, given the position and orientation of the imaging
plane (attached to the ICE probe).
""")
        self.parent.acknowledgementText = _("""
This file was originally developed as part of the ICERecon extension.
""")


#
# ICESimParameterNode
#

@parameterNodeWrapper
class ICESimParameterNode:
    """
    The parameters needed by module.

    inputSegmentation - The 3D segmentation of the heart chambers and vessels.
    imagingPlaneTransform - The linear transform representing the position and
        orientation of the imaging plane (attached to the ICE probe).
    matrixSizeX - Number of pixels of the simulated image along X.
    matrixSizeY - Number of pixels of the simulated image along Y.
    pixelSpacingX - Pixel spacing of the simulated image along X (mm).
    pixelSpacingY - Pixel spacing of the simulated image along Y (mm).
    outputVolume - The simulated ICE image.
    """
    inputSegmentation: vtkMRMLSegmentationNode
    imagingPlaneTransform: vtkMRMLLinearTransformNode
    matrixSizeX: Annotated[int, WithinRange(16, 1024)] = 256
    matrixSizeY: Annotated[int, WithinRange(16, 1024)] = 256
    pixelSpacingX: Annotated[float, WithinRange(0.01, 10.0)] = 0.5
    pixelSpacingY: Annotated[float, WithinRange(0.01, 10.0)] = 0.5
    outputVolume: vtkMRMLScalarVolumeNode


#
# ICESimWidget
#

class ICESimWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """Uses ScriptedLoadableModuleWidget base class, available at:
    https://github.com/Slicer/Slicer/blob/main/Base/Python/slicer/ScriptedLoadableModule.py
    """

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)
        self.logic = None
        self._parameterNode = None
        self._parameterNodeGuiTag = None

    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)

        uiWidget = slicer.util.loadUI(self.resourcePath("UI/ICESim.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        uiWidget.setMRMLScene(slicer.mrmlScene)

        self.logic = ICESimLogic()

        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        self.ui.applyButton.connect("clicked(bool)", self.onApplyButton)

        # connectGui() does not reliably bind qMRMLNodeComboBox widgets in
        # this Slicer build (its GUI tag comes back as 0, and selections
        # never reach the parameter node), so wire these three explicitly.
        self.ui.inputSegmentation.connect(
            "currentNodeChanged(vtkMRMLNode*)", self.onInputSegmentationChanged)
        self.ui.imagingPlaneTransform.connect(
            "currentNodeChanged(vtkMRMLNode*)", self.onImagingPlaneTransformChanged)
        self.ui.outputVolume.connect(
            "currentNodeChanged(vtkMRMLNode*)", self.onOutputVolumeChanged)

        self.initializeParameterNode()

    def cleanup(self):
        self.removeObservers()

    def enter(self):
        self.initializeParameterNode()

    def exit(self):
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
            self._parameterNodeGuiTag = None

    def onSceneStartClose(self, caller, event):
        self.setParameterNode(None)

    def onSceneEndClose(self, caller, event):
        if self.parent.isEntered:
            self.initializeParameterNode()

    def initializeParameterNode(self):
        self.setParameterNode(self.logic.getParameterNode())

    def setParameterNode(self, inputParameterNode):
        if self._parameterNode:
            self._parameterNode.disconnectGui(self._parameterNodeGuiTag)
        self._parameterNode = inputParameterNode
        if self._parameterNode:
            self._parameterNodeGuiTag = self._parameterNode.connectGui(self.ui)
            # connectGui() does not push the node-reference parameters onto
            # the qMRMLNodeComboBox widgets in this Slicer build, so set the
            # widgets' initial state from the parameter node here.
            self.ui.inputSegmentation.setCurrentNode(self._parameterNode.inputSegmentation)
            self.ui.imagingPlaneTransform.setCurrentNode(self._parameterNode.imagingPlaneTransform)
            self.ui.outputVolume.setCurrentNode(self._parameterNode.outputVolume)

    def onInputSegmentationChanged(self, node):
        if self._parameterNode:
            self._parameterNode.inputSegmentation = node

    def onImagingPlaneTransformChanged(self, node):
        if self._parameterNode:
            self._parameterNode.imagingPlaneTransform = node

    def onOutputVolumeChanged(self, node):
        if self._parameterNode:
            self._parameterNode.outputVolume = node

    def onApplyButton(self):
        with slicer.util.tryWithErrorDisplay(_("Failed to compute results."), waitCursor=True):
            self.logic.process(self._parameterNode)


#
# ICESimLogic
#

class ICESimLogic(ScriptedLoadableModuleLogic):
    """This class implements all the actual computation done by the module.
    """

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)

    def getParameterNode(self):
        return ICESimParameterNode(super().getParameterNode())

    # Approximate intensities and speckle amounts for the two simulated
    # tissue classes. These are not exposed in the GUI yet; revisit once the
    # visual result has been validated against real ICE images.
    TISSUE_INTENSITY = 190.0
    TISSUE_NOISE_SIGMA = 0.18  # relative (multiplicative) speckle
    BLOOD_INTENSITY = 15.0
    BLOOD_NOISE_SIGMA = 0.35

    def process(self, parameterNode: ICESimParameterNode):
        """Generate the simulated ICE image.

        Takes the cross section of the input segmentation along the imaging
        plane and renders a simulated ultrasound image (dark blood pool,
        bright tissue, with added noise).
        """
        if not parameterNode.inputSegmentation:
            raise ValueError("Input segmentation is not set.")
        if not parameterNode.imagingPlaneTransform:
            raise ValueError("Imaging plane transform is not set.")
        if not parameterNode.outputVolume:
            raise ValueError("Output volume is not set.")

        sizeX = parameterNode.matrixSizeX
        sizeY = parameterNode.matrixSizeY
        spacingX = parameterNode.pixelSpacingX
        spacingY = parameterNode.pixelSpacingY

        startTime = time.time()
        logging.info("ICESim processing started")

        ijkToRAS = self._computeIJKToRAS(parameterNode.imagingPlaneTransform, sizeX, sizeY, spacingX, spacingY)
        bloodPoolMask = self._resliceSegmentationToPlane(
            parameterNode.inputSegmentation, ijkToRAS, sizeX, sizeY)
        imageArray = self._renderUltrasoundImage(bloodPoolMask)
        self._updateOutputVolume(parameterNode.outputVolume, imageArray, ijkToRAS)

        logging.info(f"ICESim processing completed in {time.time() - startTime:.2f} seconds")

    @staticmethod
    def _computeIJKToRAS(transformNode, sizeX, sizeY, spacingX, spacingY):
        """Build the IJK-to-RAS matrix of the simulated image.

        The imaging plane's X/Y axes and origin are taken from the transform
        node (Z is the plane normal). Row j=0 corresponds to the probe
        (depth 0), with depth increasing along +Y; column i is centered
        laterally around the transform's origin.
        """
        transformToWorld = vtk.vtkMatrix4x4()
        transformNode.GetMatrixTransformToWorld(transformToWorld)

        offset = vtk.vtkMatrix4x4()
        offset.Identity()
        offset.SetElement(0, 0, spacingX)
        offset.SetElement(1, 1, spacingY)
        offset.SetElement(0, 3, -((sizeX - 1) / 2.0) * spacingX)

        ijkToRAS = vtk.vtkMatrix4x4()
        vtk.vtkMatrix4x4.Multiply4x4(transformToWorld, offset, ijkToRAS)
        return ijkToRAS

    @staticmethod
    def _resliceSegmentationToPlane(segmentationNode, ijkToRAS, sizeX, sizeY):
        """Reslice the (merged) segmentation labelmap onto the imaging plane.

        Returns a boolean numpy array of shape (sizeY, sizeX) that is True
        inside the blood pool (i.e. inside any visible segment).
        """
        labelmapVolumeNode = slicer.mrmlScene.AddNewNodeByClass(
            "vtkMRMLLabelMapVolumeNode", "ICESim_TempLabelmap")
        try:
            segmentationsLogic = slicer.modules.segmentations.logic()
            success = segmentationsLogic.ExportVisibleSegmentsToLabelmapNode(
                segmentationNode, labelmapVolumeNode)
            if not success:
                raise RuntimeError("Failed to export segmentation to a labelmap.")

            inputImageData = labelmapVolumeNode.GetImageData()
            rasToIJK = vtk.vtkMatrix4x4()
            labelmapVolumeNode.GetRASToIJKMatrix(rasToIJK)

            resliceAxes = vtk.vtkMatrix4x4()
            vtk.vtkMatrix4x4.Multiply4x4(rasToIJK, ijkToRAS, resliceAxes)

            reslice = vtk.vtkImageReslice()
            reslice.SetInputData(inputImageData)
            reslice.SetResliceAxes(resliceAxes)
            reslice.SetOutputExtent(0, sizeX - 1, 0, sizeY - 1, 0, 0)
            reslice.SetOutputSpacing(1.0, 1.0, 1.0)
            reslice.SetOutputOrigin(0.0, 0.0, 0.0)
            reslice.SetInterpolationModeToNearestNeighbor()
            reslice.SetBackgroundLevel(0)
            reslice.Update()

            maskArray = vtk_np.vtk_to_numpy(reslice.GetOutput().GetPointData().GetScalars())
            maskArray = maskArray.reshape(sizeY, sizeX)
            return maskArray != 0
        finally:
            slicer.mrmlScene.RemoveNode(labelmapVolumeNode)

    @classmethod
    def _renderUltrasoundImage(cls, bloodPoolMask):
        """Render tissue/blood intensities with multiplicative speckle noise."""
        rng = np.random.default_rng()

        baseImage = np.where(bloodPoolMask, cls.BLOOD_INTENSITY, cls.TISSUE_INTENSITY)
        noiseSigma = np.where(bloodPoolMask, cls.BLOOD_NOISE_SIGMA, cls.TISSUE_NOISE_SIGMA)
        speckle = rng.normal(0.0, 1.0, size=baseImage.shape)

        image = baseImage * (1.0 + noiseSigma * speckle)
        image = np.clip(image, 0, 255)
        return image.astype(np.uint8)

    @staticmethod
    def _updateOutputVolume(outputVolumeNode, imageArray, ijkToRAS):
        sizeY, sizeX = imageArray.shape

        imageData = vtk.vtkImageData()
        imageData.SetDimensions(sizeX, sizeY, 1)
        imageData.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 1)

        vtkArray = vtk_np.numpy_to_vtk(imageArray.reshape(-1), deep=True, array_type=vtk.VTK_UNSIGNED_CHAR)
        imageData.GetPointData().SetScalars(vtkArray)

        outputVolumeNode.SetAndObserveImageData(imageData)
        outputVolumeNode.SetIJKToRASMatrix(ijkToRAS)

        outputVolumeNode.CreateDefaultDisplayNodes()
        displayNode = outputVolumeNode.GetDisplayNode()
        if displayNode:
            displayNode.SetAutoWindowLevel(True)


#
# ICESimTest
#

class ICESimTest(ScriptedLoadableModuleTest):
    """
    This is the test case for your scripted module.
    """

    def setUp(self):
        slicer.mrmlScene.Clear()

    def runTest(self):
        self.setUp()
        self.test_ICESim1()

    def test_ICESim1(self):
        self.delayDisplay("No automated tests implemented yet")
        self.delayDisplay("Test passed")
