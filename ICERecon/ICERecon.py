import logging
import math
import os

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

from typing import Annotated

from slicer import vtkMRMLScalarVolumeNode
from slicer import vtkMRMLMarkupsROINode


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
Reconstructs a 3D volume from a 2D scalar volume (e.g. a simulated or live ICE
image) that moves through space over time. As the input volume moves, ICERecon
paints the region of the output volume currently overlapped by the input with
the input's pixel values, building up a 3D reconstruction as the input sweeps.
""")
        self.parent.acknowledgementText = _("""
This file was originally developed as part of the ICERecon extension.
""")


#
# ICEReconParameterNode
#

@parameterNodeWrapper
class ICEReconParameterNode:
    """
    The parameters needed by module.

    inputVolume - The moving 2D (or thin) scalar volume to paint into the
        output volume, e.g. ICESim's output volume or a live tracked image.
    outputVolume - The 3D scalar volume being reconstructed. Must be
        initialized (see boundingBox/outputSpacingMm) before painting.
    boundingBox - The ROI defining the physical extent of the output volume.
    outputSpacingMm - Isotropic voxel spacing (mm) of the output volume, used
        when initializing it from boundingBox.
    intensityScale - Multiplier applied to the input's pixel values before
        painting them into the output volume (clipped to the output's valid
        range). Applies to subsequent paints only, not retroactively to
        voxels already painted.
    """
    inputVolume: vtkMRMLScalarVolumeNode
    outputVolume: vtkMRMLScalarVolumeNode
    boundingBox: vtkMRMLMarkupsROINode
    outputSpacingMm: Annotated[float, WithinRange(0.05, 10.0)] = 1.0
    intensityScale: Annotated[float, WithinRange(0.01, 10.0)] = 1.0


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
        self._parameterNode = None
        self._parameterNodeGuiTag = None
        self._observedInputVolumeNode = None

    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)

        uiWidget = slicer.util.loadUI(self.resourcePath("UI/ICERecon.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)

        uiWidget.setMRMLScene(slicer.mrmlScene)

        self.logic = ICEReconLogic()

        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)

        self.ui.applyButton.connect("clicked(bool)", self.onApplyButton)
        self.ui.initializeButton.connect("clicked(bool)", self.onInitializeButton)

        # connectGui() does not reliably bind widgets to the parameter node
        # in this Slicer build (see ICESim), so wire every widget explicitly.
        self.ui.inputVolume.connect(
            "currentNodeChanged(vtkMRMLNode*)", self.onInputVolumeChanged)
        self.ui.outputVolume.connect(
            "currentNodeChanged(vtkMRMLNode*)", self.onOutputVolumeChanged)
        self.ui.boundingBox.connect(
            "currentNodeChanged(vtkMRMLNode*)", self.onBoundingBoxChanged)
        self.ui.outputSpacingMm.connect(
            "valueChanged(double)", self.onOutputSpacingMmChanged)
        self.ui.intensityScale.connect(
            "valueChanged(double)", self.onIntensityScaleChanged)

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
            self.ui.inputVolume.setCurrentNode(self._parameterNode.inputVolume)
            self.ui.outputVolume.setCurrentNode(self._parameterNode.outputVolume)
            self.ui.boundingBox.setCurrentNode(self._parameterNode.boundingBox)
            self.ui.outputSpacingMm.setValue(self._parameterNode.outputSpacingMm)
            self.ui.intensityScale.setValue(self._parameterNode.intensityScale)
            self._setObservedInputVolumeNode(self._parameterNode.inputVolume)
        else:
            self._setObservedInputVolumeNode(None)

    def _setObservedInputVolumeNode(self, volumeNode):
        """Track changes to the input volume -- both direct modifications
        (e.g. IJKToRAS/pixel updates, as ICESim's output volume moves) and
        changes bubbled from a parent transform (e.g. a live tracked image
        moved by a separate transform node) -- so the output volume is
        repainted automatically whenever the input volume moves.
        """
        if self._observedInputVolumeNode is not None:
            self.removeObserver(
                self._observedInputVolumeNode, vtk.vtkCommand.ModifiedEvent, self.onInputVolumeModified)
            self.removeObserver(
                self._observedInputVolumeNode,
                slicer.vtkMRMLTransformableNode.TransformModifiedEvent,
                self.onInputVolumeModified)
        self._observedInputVolumeNode = volumeNode
        if self._observedInputVolumeNode is not None:
            self.addObserver(
                self._observedInputVolumeNode, vtk.vtkCommand.ModifiedEvent, self.onInputVolumeModified)
            self.addObserver(
                self._observedInputVolumeNode,
                slicer.vtkMRMLTransformableNode.TransformModifiedEvent,
                self.onInputVolumeModified)

    def onInputVolumeModified(self, caller, event):
        self._autoUpdatePaint()

    def _autoUpdatePaint(self):
        if not self._parameterNode:
            return
        if not (self._parameterNode.inputVolume
                and self._parameterNode.outputVolume
                and self._parameterNode.boundingBox):
            return
        if not self._parameterNode.outputVolume.GetImageData():
            return  # not initialized yet
        try:
            self.logic.paint(
                self._parameterNode.inputVolume, self._parameterNode.outputVolume,
                self._parameterNode.intensityScale)
        except Exception as e:
            logging.error(f"ICERecon: automatic paint failed: {e}")

    def onInputVolumeChanged(self, node):
        if self._parameterNode:
            self._parameterNode.inputVolume = node
        self._setObservedInputVolumeNode(node)

    def onOutputVolumeChanged(self, node):
        if self._parameterNode:
            self._parameterNode.outputVolume = node

    def onBoundingBoxChanged(self, node):
        if self._parameterNode:
            self._parameterNode.boundingBox = node

    def onOutputSpacingMmChanged(self, value):
        if self._parameterNode:
            self._parameterNode.outputSpacingMm = value

    def onIntensityScaleChanged(self, value):
        if self._parameterNode:
            self._parameterNode.intensityScale = value

    def onInitializeButton(self):
        with slicer.util.tryWithErrorDisplay(_("Failed to initialize output volume."), waitCursor=True):
            self.logic.initializeOutputVolume(
                self._parameterNode.outputVolume,
                self._parameterNode.boundingBox,
                self._parameterNode.outputSpacingMm)
        # Paint immediately so the volume reflects the input's current
        # position right away, rather than staying blank until it next moves.
        self._autoUpdatePaint()

    def onApplyButton(self):
        with slicer.util.tryWithErrorDisplay(_("Failed to paint output volume."), waitCursor=True):
            self.logic.paint(
                self._parameterNode.inputVolume, self._parameterNode.outputVolume,
                self._parameterNode.intensityScale)


#
# ICEReconLogic
#

class ICEReconLogic(ScriptedLoadableModuleLogic):
    """This class implements all the actual computation done by the module.
    """

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)

    def getParameterNode(self):
        return ICEReconParameterNode(super().getParameterNode())

    def initializeOutputVolume(self, outputVolumeNode, boundingBoxNode, spacingMm):
        """(Re)allocate the output volume to cover boundingBoxNode at
        spacingMm isotropic resolution, filled with zero. This is a
        destructive reset: any previously reconstructed data is discarded.
        The output volume is always RAS-axis-aligned, regardless of any
        rotation applied to the ROI's own interaction handles.
        """
        if not outputVolumeNode:
            raise ValueError("Output volume is not set.")
        if not boundingBoxNode:
            raise ValueError("Bounding box is not set.")

        center = [0.0, 0.0, 0.0]
        boundingBoxNode.GetCenterWorld(center)
        size = list(boundingBoxNode.GetSize())

        dims = [max(1, int(math.ceil(size[i] / spacingMm))) for i in range(3)]
        origin = [center[i] - size[i] / 2.0 for i in range(3)]

        imageData = vtk.vtkImageData()
        imageData.SetDimensions(dims[0], dims[1], dims[2])
        imageData.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 1)
        imageData.GetPointData().GetScalars().Fill(0)

        ijkToRAS = vtk.vtkMatrix4x4()
        ijkToRAS.Identity()
        for r in range(3):
            ijkToRAS.SetElement(r, r, spacingMm)
            ijkToRAS.SetElement(r, 3, origin[r])

        outputVolumeNode.SetAndObserveImageData(imageData)
        outputVolumeNode.SetIJKToRASMatrix(ijkToRAS)
        outputVolumeNode.CreateDefaultDisplayNodes()

        logging.info(f"ICERecon: initialized output volume, dimensions={dims}, spacing={spacingMm}mm")

    def paint(self, inputVolumeNode, outputVolumeNode, intensityScale=1.0):
        """Paint the region of outputVolumeNode currently overlapped by
        inputVolumeNode with inputVolumeNode's pixel values times
        intensityScale, clipped to the output's valid range (last-write-wins,
        no blending). Voxels outside the input's current extent are left
        unchanged, so the reconstruction accumulates as the input moves.
        """
        if not inputVolumeNode:
            raise ValueError("Input volume is not set.")
        if not outputVolumeNode:
            raise ValueError("Output volume is not set.")

        outputImageData = outputVolumeNode.GetImageData()
        if not outputImageData:
            raise ValueError('Output volume is not initialized. Click "Initialize Output Volume" first.')

        inputImageData = inputVolumeNode.GetImageData()
        if not inputImageData:
            return  # nothing to paint yet

        # Input's true world-space IJK-to-RAS: its own IJKToRAS combined with
        # its parent transform's world transform, if any (same technique
        # used for imagingPlaneTransform in ICESim).
        inputIJKToRAS = vtk.vtkMatrix4x4()
        inputVolumeNode.GetIJKToRASMatrix(inputIJKToRAS)
        parentTransformNode = inputVolumeNode.GetParentTransformNode()
        if parentTransformNode:
            transformToWorld = vtk.vtkMatrix4x4()
            parentTransformNode.GetMatrixTransformToWorld(transformToWorld)
            inputIJKToWorld = vtk.vtkMatrix4x4()
            vtk.vtkMatrix4x4.Multiply4x4(transformToWorld, inputIJKToRAS, inputIJKToWorld)
        else:
            inputIJKToWorld = inputIJKToRAS

        inputWorldToIJK = vtk.vtkMatrix4x4()
        vtk.vtkMatrix4x4.Invert(inputIJKToWorld, inputWorldToIJK)

        outputIJKToRAS = vtk.vtkMatrix4x4()
        outputVolumeNode.GetIJKToRASMatrix(outputIJKToRAS)

        # Maps output IJK -> input IJK.
        resliceAxes = vtk.vtkMatrix4x4()
        vtk.vtkMatrix4x4.Multiply4x4(inputWorldToIJK, outputIJKToRAS, resliceAxes)

        dims = outputImageData.GetDimensions()

        dataArray = self._resliceToOutputGrid(inputImageData, resliceAxes, dims)

        inputDims = inputImageData.GetDimensions()
        validityImage = vtk.vtkImageData()
        validityImage.SetDimensions(inputDims[0], inputDims[1], inputDims[2])
        validityImage.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 1)
        validityImage.GetPointData().GetScalars().Fill(1)
        hitMask = self._resliceToOutputGrid(validityImage, resliceAxes, dims)

        outputArray = slicer.util.arrayFromVolume(outputVolumeNode)
        hit = hitMask != 0
        valueRange = np.iinfo(outputArray.dtype) if np.issubdtype(outputArray.dtype, np.integer) else None
        scaledData = dataArray.astype(np.float64) * intensityScale
        if valueRange is not None:
            scaledData = np.clip(scaledData, valueRange.min, valueRange.max)
        outputArray[hit] = scaledData[hit].astype(outputArray.dtype)
        slicer.util.arrayFromVolumeModified(outputVolumeNode)

    @staticmethod
    def _resliceToOutputGrid(imageData, resliceAxes, dims):
        """Reslice imageData (in its own pure-index space: spacing 1, origin
        0, matching the MRML convention where real geometry lives in the
        node's IJKToRAS rather than the vtkImageData itself) through
        resliceAxes onto a grid of size dims, nearest-neighbor, background 0.
        Returns a numpy array of shape (dims[2], dims[1], dims[0]).
        """
        reslice = vtk.vtkImageReslice()
        reslice.SetInputData(imageData)
        reslice.SetResliceAxes(resliceAxes)
        reslice.SetOutputExtent(0, dims[0] - 1, 0, dims[1] - 1, 0, dims[2] - 1)
        reslice.SetOutputSpacing(1.0, 1.0, 1.0)
        reslice.SetOutputOrigin(0.0, 0.0, 0.0)
        reslice.SetInterpolationModeToNearestNeighbor()
        reslice.SetBackgroundLevel(0)
        reslice.Update()

        array = vtk_np.vtk_to_numpy(reslice.GetOutput().GetPointData().GetScalars())
        return array.reshape(dims[2], dims[1], dims[0])


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
