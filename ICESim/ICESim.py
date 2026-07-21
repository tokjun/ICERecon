import logging
import math
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
    scanPlaneOrientation - "Perpendicular": the imaging plane is perpendicular
        to the transform's third column vector (the catheter axis), i.e. the
        catheter axis is the plane normal. "Parallel (forward)": the imaging
        plane contains the catheter axis instead, extending forward from the
        tip. "Parallel (side)": the "Parallel (forward)" plane rotated 90
        degrees about the transform's second column vector (perpendicular
        to the catheter), pivoting on the tip (the transform's fourth
        column vector), so the near edge of the image runs along the
        catheter.
    viewAngleDeg - Full opening angle (degrees) of the fan-shaped imaging
        area. The fan's apex is at the center of the image's near edge
        (row 0, the catheter), opening symmetrically toward increasing
        depth; pixels outside the fan are set to zero.
    minRangeMm - Minimum radial distance (mm) from the fan's apex that is
        rendered; pixels closer than this are set to zero.
    maxRangeMm - Maximum radial distance (mm) from the fan's apex that is
        rendered; pixels farther than this are set to zero.
    sweepAngleDeg - Rotation (degrees) of the imaging plane about the
        catheter axis (the imagingPlaneTransform's third column vector),
        applied on top of that transform without modifying it. Lets the
        imaging plane be swept around the catheter interactively (e.g. to
        preview a mechanically rotated or side-firing catheter's sweep)
        without altering the tracked transform node itself.
    matrixSizeX - Number of pixels of the simulated image along X.
    matrixSizeY - Number of pixels of the simulated image along Y.
    pixelSpacingX - Pixel spacing of the simulated image along X (mm).
    pixelSpacingY - Pixel spacing of the simulated image along Y (mm).
    outputVolume - The simulated grayscale ultrasound image, with speckle
        noise. Optional if outputSegmentation is set.
    outputSegmentation - Each structure in the segmentation keeps its own
        exported label value (0 = background), with no noise added -- the
        same cross-section as outputVolume, just rendered as clean labels
        instead of simulated ultrasound. Can be either a
        vtkMRMLLabelMapVolumeNode (gets automatic per-label color display)
        or a plain vtkMRMLScalarVolumeNode (e.g. for OpenIGTLink
        compatibility, which doesn't handle label map volumes). Optional if
        outputVolume is set. Both outputs, if set, are generated on every
        Apply/auto-update, from the same geometry.
    """
    inputSegmentation: vtkMRMLSegmentationNode
    imagingPlaneTransform: vtkMRMLLinearTransformNode
    scanPlaneOrientation: str = "Perpendicular"
    viewAngleDeg: Annotated[float, WithinRange(1.0, 179.0)] = 90.0
    minRangeMm: Annotated[float, WithinRange(0.0, 1000.0)] = 0.0
    maxRangeMm: Annotated[float, WithinRange(0.0, 1000.0)] = 200.0
    sweepAngleDeg: Annotated[float, WithinRange(-180.0, 180.0)] = 0.0
    matrixSizeX: Annotated[int, WithinRange(16, 1024)] = 256
    matrixSizeY: Annotated[int, WithinRange(16, 1024)] = 256
    pixelSpacingX: Annotated[float, WithinRange(0.01, 10.0)] = 0.5
    pixelSpacingY: Annotated[float, WithinRange(0.01, 10.0)] = 0.5
    outputVolume: vtkMRMLScalarVolumeNode
    outputSegmentation: vtkMRMLScalarVolumeNode


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
        self._observedTransformNode = None

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

        # connectGui() does not reliably bind widgets to the parameter node
        # in this Slicer build (its GUI tag comes back as 0, and edits never
        # reach the parameter node), so wire every widget explicitly.
        self.ui.inputSegmentation.connect(
            "currentNodeChanged(vtkMRMLNode*)", self.onInputSegmentationChanged)
        self.ui.imagingPlaneTransform.connect(
            "currentNodeChanged(vtkMRMLNode*)", self.onImagingPlaneTransformChanged)
        self.ui.outputVolume.connect(
            "currentNodeChanged(vtkMRMLNode*)", self.onOutputVolumeChanged)
        self.ui.outputSegmentation.connect(
            "currentNodeChanged(vtkMRMLNode*)", self.onOutputSegmentationChanged)
        self.ui.scanPlaneOrientation.connect(
            "currentTextChanged(QString)", self.onScanPlaneOrientationChanged)
        self.ui.viewAngleDeg.connect(
            "valueChanged(double)", self.onViewAngleDegChanged)
        self.ui.minRangeMm.connect(
            "valueChanged(double)", self.onMinRangeMmChanged)
        self.ui.maxRangeMm.connect(
            "valueChanged(double)", self.onMaxRangeMmChanged)
        self.ui.matrixSizeX.connect(
            "valueChanged(int)", self.onMatrixSizeXChanged)
        self.ui.matrixSizeY.connect(
            "valueChanged(int)", self.onMatrixSizeYChanged)
        self.ui.pixelSpacingX.connect(
            "valueChanged(double)", self.onPixelSpacingXChanged)
        self.ui.pixelSpacingY.connect(
            "valueChanged(double)", self.onPixelSpacingYChanged)
        self.ui.sweepAngleDeg.connect(
            "valueChanged(double)", self.onSweepAngleDegChanged)

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
            # the widgets in this Slicer build, so set the widgets' initial
            # state from the parameter node here.
            self.ui.inputSegmentation.setCurrentNode(self._parameterNode.inputSegmentation)
            self.ui.imagingPlaneTransform.setCurrentNode(self._parameterNode.imagingPlaneTransform)
            self.ui.outputVolume.setCurrentNode(self._parameterNode.outputVolume)
            self.ui.outputSegmentation.setCurrentNode(self._parameterNode.outputSegmentation)
            self.ui.scanPlaneOrientation.setCurrentText(self._parameterNode.scanPlaneOrientation)
            self.ui.viewAngleDeg.setValue(self._parameterNode.viewAngleDeg)
            self.ui.minRangeMm.setValue(self._parameterNode.minRangeMm)
            self.ui.maxRangeMm.setValue(self._parameterNode.maxRangeMm)
            self.ui.matrixSizeX.setValue(self._parameterNode.matrixSizeX)
            self.ui.matrixSizeY.setValue(self._parameterNode.matrixSizeY)
            self.ui.pixelSpacingX.setValue(self._parameterNode.pixelSpacingX)
            self.ui.pixelSpacingY.setValue(self._parameterNode.pixelSpacingY)
            self.ui.sweepAngleDeg.setValue(self._parameterNode.sweepAngleDeg)
            self._setObservedTransformNode(self._parameterNode.imagingPlaneTransform)
        else:
            self._setObservedTransformNode(None)

    def _setObservedTransformNode(self, transformNode):
        """Track TransformModifiedEvent on the imaging plane transform so the
        output volume is regenerated whenever the probe pose changes."""
        if self._observedTransformNode is not None:
            self.removeObserver(
                self._observedTransformNode,
                slicer.vtkMRMLTransformNode.TransformModifiedEvent,
                self.onImagingPlaneTransformModified)
        self._observedTransformNode = transformNode
        if self._observedTransformNode is not None:
            self.addObserver(
                self._observedTransformNode,
                slicer.vtkMRMLTransformNode.TransformModifiedEvent,
                self.onImagingPlaneTransformModified)

    def onImagingPlaneTransformModified(self, caller, event):
        self._autoUpdateOutputVolume()

    def _autoUpdateOutputVolume(self):
        if not self._parameterNode:
            return
        if not (self._parameterNode.inputSegmentation
                and self._parameterNode.imagingPlaneTransform
                and (self._parameterNode.outputVolume or self._parameterNode.outputSegmentation)):
            return
        try:
            self.logic.process(self._parameterNode)
        except Exception as e:
            logging.error(f"ICESim: automatic update failed: {e}")

    def onInputSegmentationChanged(self, node):
        if self._parameterNode:
            self._parameterNode.inputSegmentation = node

    def onImagingPlaneTransformChanged(self, node):
        if self._parameterNode:
            self._parameterNode.imagingPlaneTransform = node
        self._setObservedTransformNode(node)

    def onOutputVolumeChanged(self, node):
        if self._parameterNode:
            self._parameterNode.outputVolume = node

    def onOutputSegmentationChanged(self, node):
        if self._parameterNode:
            self._parameterNode.outputSegmentation = node

    def onScanPlaneOrientationChanged(self, text):
        if self._parameterNode:
            self._parameterNode.scanPlaneOrientation = text

    def onViewAngleDegChanged(self, value):
        if self._parameterNode:
            self._parameterNode.viewAngleDeg = value

    def onMinRangeMmChanged(self, value):
        if self._parameterNode:
            self._parameterNode.minRangeMm = value

    def onMaxRangeMmChanged(self, value):
        if self._parameterNode:
            self._parameterNode.maxRangeMm = value

    def onMatrixSizeXChanged(self, value):
        if self._parameterNode:
            self._parameterNode.matrixSizeX = value

    def onMatrixSizeYChanged(self, value):
        if self._parameterNode:
            self._parameterNode.matrixSizeY = value

    def onPixelSpacingXChanged(self, value):
        if self._parameterNode:
            self._parameterNode.pixelSpacingX = value

    def onPixelSpacingYChanged(self, value):
        if self._parameterNode:
            self._parameterNode.pixelSpacingY = value

    def onSweepAngleDegChanged(self, value):
        if self._parameterNode:
            self._parameterNode.sweepAngleDeg = value
        # The sweep slider is meant for interactive scrubbing, so update
        # live rather than waiting for the user to click Apply.
        self._autoUpdateOutputVolume()

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

    # Speckle grain size (pixels), i.e. the spatial correlation length of
    # the noise: real ultrasound speckle isn't per-pixel-independent, its
    # grain size is set by the beam width (lateral) and pulse length
    # (axial), which are usually anisotropic -- lateral resolution is
    # coarser than axial, so grains are wider than they are tall.
    SPECKLE_SIGMA_AXIAL_PX = 0.8
    SPECKLE_SIGMA_LATERAL_PX = 1.8

    def process(self, parameterNode: ICESimParameterNode):
        """Generate the simulated ICE image(s).

        Takes the cross section of the input segmentation along the imaging
        plane and, for each output that is set, renders it: outputVolume
        gets a simulated ultrasound image (dark blood pool, bright tissue,
        with added noise); outputSegmentation gets the same cross-section
        as clean per-structure labels, with no noise. Both are generated
        from the same geometry whenever both are set.
        """
        if not parameterNode.inputSegmentation:
            raise ValueError("Input segmentation is not set.")
        if not parameterNode.imagingPlaneTransform:
            raise ValueError("Imaging plane transform is not set.")
        if not (parameterNode.outputVolume or parameterNode.outputSegmentation):
            raise ValueError("At least one of Output volume or Output segmentation must be set.")

        sizeX = parameterNode.matrixSizeX
        sizeY = parameterNode.matrixSizeY
        spacingX = parameterNode.pixelSpacingX
        spacingY = parameterNode.pixelSpacingY
        viewAngleDeg = parameterNode.viewAngleDeg
        minRangeMm = parameterNode.minRangeMm
        maxRangeMm = parameterNode.maxRangeMm

        startTime = time.time()
        logging.info("ICESim processing started")

        # depthOffsetMm shapes which pixels the fan mask shows (see
        # _computeDepthOffsetMm) but is intentionally NOT applied to
        # ijkToRAS: row 0 must stay physically anchored at the transform's
        # origin (the catheter tip) so the image edge stays in contact
        # with the catheter in the scene, rather than floating away from
        # it by depthOffsetMm.
        depthOffsetMm = self._computeDepthOffsetMm(viewAngleDeg, minRangeMm, spacingY)
        ijkToRAS = self._computeIJKToRAS(
            parameterNode.imagingPlaneTransform, sizeX, sizeY, spacingX, spacingY,
            parameterNode.scanPlaneOrientation, parameterNode.sweepAngleDeg)
        labelArray = self._resliceSegmentationToPlane(
            parameterNode.inputSegmentation, ijkToRAS, sizeX, sizeY)
        fanMask = self._computeFanMask(
            sizeX, sizeY, spacingX, spacingY, viewAngleDeg, minRangeMm, maxRangeMm, depthOffsetMm)

        if parameterNode.outputVolume:
            bloodPoolMask = labelArray != 0
            imageArray = self._renderUltrasoundImage(bloodPoolMask)
            imageArray[~fanMask] = 0
            self._updateOutputVolume(parameterNode.outputVolume, imageArray, ijkToRAS)

        if parameterNode.outputSegmentation:
            segmentationArray = self._renderSegmentationImage(labelArray)
            segmentationArray[~fanMask] = 0
            self._updateOutputVolume(parameterNode.outputSegmentation, segmentationArray, ijkToRAS)

        logging.info(f"ICESim processing completed in {time.time() - startTime:.2f} seconds")

    @staticmethod
    def _computeDepthOffsetMm(viewAngleDeg, minRangeMm, spacingY):
        """Depth (mm, from the true apex/catheter tip) that image row j=0
        corresponds to.

        With no offset, row 0 sits exactly at the apex. But the fan is
        very narrow near the apex, so as soon as a near-range cutoff
        (minRangeMm) excludes anything, it excludes the *entire* cone
        cross-section for a band of depths right below the apex (the cone
        edges only clear the exclusion circle once depth >=
        minRangeMm * cos(halfAngle)) -- so the fan's two straight edges
        would not reach the image's near edge, leaving it blank instead of
        touching the frame like a real ultrasound sector display.

        Cropping row 0 to that depth (plus one extra pixel row of margin,
        since exactly at it the edges are merely tangent to the exclusion
        circle and may be lost to rounding) makes the two edges -- and the
        rounded near-field cutout between them -- touch the image's near
        edge, matching a real sector display.
        """
        if minRangeMm <= 0:
            return 0.0
        halfAngleRad = math.radians(viewAngleDeg / 2.0)
        return minRangeMm * math.cos(halfAngleRad) + spacingY

    @staticmethod
    def _computeIJKToRAS(transformNode, sizeX, sizeY, spacingX, spacingY, orientation="Perpendicular",
                          sweepAngleDeg=0.0):
        """Build the IJK-to-RAS matrix of the simulated image.

        sweepAngleDeg rotates the plane about the transform's third column
        vector (the catheter axis) before orientation is applied, without
        modifying transformNode itself -- this is what lets a "Sweeping"
        slider preview different rotations of the imaging plane around the
        catheter without touching the tracked imagingPlaneTransform node.

        Row j=0 corresponds to the probe (depth 0) exactly -- the image's
        near edge is always physically anchored at the transform's origin
        (the catheter tip), so it stays in contact with the catheter in
        the scene. (_computeFanMask separately crops which pixels are
        shown so the fan's near edges appear to touch that same row 0; see
        _computeDepthOffsetMm for why those are deliberately different.)
        Depth increases along +j; column i is centered laterally around
        the origin. Depending on orientation:

        - "Perpendicular" (default): the plane normal is the transform's
          third column vector (the catheter axis), so the imaging plane is
          a cross-section perpendicular to the catheter.
          i = transform X, j = transform Y, k = transform Z.
        - "Parallel (forward)": the imaging plane contains the catheter
          axis instead, extending forward from the tip.
          i = transform X, j = transform Z, k = transform Y.
        - "Parallel (side)": the "Parallel (forward)" plane (i = transform
          X, j = transform Z, k = transform Y) rotated 90 degrees about the
          transform's second column vector (Y, perpendicular to the
          catheter), pivoting on the tip: i = -transform Z, j = transform
          X, k = transform Y (unchanged, since Y is the rotation axis).
          Because depth (j) is now perpendicular to the catheter and row
          j=0 is at the tip, the near edge of the image (row 0) runs along
          the full length of the catheter axis rather than through a
          single point on it.
        """
        transformToWorld = vtk.vtkMatrix4x4()
        transformNode.GetMatrixTransformToWorld(transformToWorld)

        xAxis = [transformToWorld.GetElement(r, 0) for r in range(3)]
        yAxis = [transformToWorld.GetElement(r, 1) for r in range(3)]
        zAxis = [transformToWorld.GetElement(r, 2) for r in range(3)]
        origin = [transformToWorld.GetElement(r, 3) for r in range(3)]

        if sweepAngleDeg:
            sweepRad = math.radians(sweepAngleDeg)
            cosT, sinT = math.cos(sweepRad), math.sin(sweepRad)
            xAxis, yAxis = (
                [xAxis[r] * cosT + yAxis[r] * sinT for r in range(3)],
                [yAxis[r] * cosT - xAxis[r] * sinT for r in range(3)],
            )

        if orientation == "Parallel (side)":
            iAxis, jAxis, kAxis = [-c for c in zAxis], xAxis, yAxis
        elif orientation == "Parallel (forward)":
            iAxis, jAxis, kAxis = xAxis, zAxis, yAxis
        else:
            iAxis, jAxis, kAxis = xAxis, yAxis, zAxis

        cx = (sizeX - 1) / 2.0
        ijkToRAS = vtk.vtkMatrix4x4()
        ijkToRAS.Identity()
        for r in range(3):
            ijkToRAS.SetElement(r, 0, iAxis[r] * spacingX)
            ijkToRAS.SetElement(r, 1, jAxis[r] * spacingY)
            ijkToRAS.SetElement(r, 2, kAxis[r])
            ijkToRAS.SetElement(r, 3, origin[r] - iAxis[r] * spacingX * cx)
        return ijkToRAS

    @staticmethod
    def _resliceSegmentationToPlane(segmentationNode, ijkToRAS, sizeX, sizeY):
        """Reslice the (merged) segmentation labelmap onto the imaging plane.

        Returns an integer numpy array of shape (sizeY, sizeX) holding each
        segment's exported label value (the same convention as
        ExportVisibleSegmentsToLabelmapNode: a distinct positive integer
        per segment, 0 = background, outside any segment).
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

            labelArray = vtk_np.vtk_to_numpy(reslice.GetOutput().GetPointData().GetScalars())
            # Copy: the array otherwise shares memory with the reslice
            # filter's output, which is discarded when this function returns.
            return labelArray.reshape(sizeY, sizeX).copy()
        finally:
            slicer.mrmlScene.RemoveNode(labelmapVolumeNode)

    @staticmethod
    def _renderSegmentationImage(labelArray):
        """Render a label image with no noise: each segment keeps its own
        exported label value (see _resliceSegmentationToPlane), 0 =
        background.
        """
        return labelArray.astype(np.uint8)

    @classmethod
    def _renderUltrasoundImage(cls, bloodPoolMask):
        """Render tissue/blood intensities with spatially-correlated speckle
        noise (rather than per-pixel-independent noise, which looks like
        static instead of the grainy texture of real ultrasound speckle).
        """
        rng = np.random.default_rng()

        baseImage = np.where(bloodPoolMask, cls.BLOOD_INTENSITY, cls.TISSUE_INTENSITY)
        noiseSigma = np.where(bloodPoolMask, cls.BLOOD_NOISE_SIGMA, cls.TISSUE_NOISE_SIGMA)

        speckle = rng.normal(0.0, 1.0, size=baseImage.shape)
        kernel = cls._gaussianKernel2D(cls.SPECKLE_SIGMA_AXIAL_PX, cls.SPECKLE_SIGMA_LATERAL_PX)
        speckle = cls._convolveSame(speckle, kernel)
        speckle /= (speckle.std() + 1e-8)  # blurring reduces variance; restore unit std

        image = baseImage * (1.0 + noiseSigma * speckle)
        image = np.clip(image, 0, 255)
        return image.astype(np.uint8)

    @staticmethod
    def _gaussianKernel2D(sigmaRows, sigmaCols):
        """Separable 2D Gaussian kernel (outer product of two 1D kernels),
        one sigma (pixels) per axis so the blur can be anisotropic.
        """
        def axisKernel(sigma):
            if sigma <= 1e-6:
                return np.array([1.0])
            radius = max(1, int(round(3 * sigma)))
            x = np.arange(-radius, radius + 1)
            k = np.exp(-(x ** 2) / (2.0 * sigma ** 2))
            return k / k.sum()

        return np.outer(axisKernel(sigmaRows), axisKernel(sigmaCols))

    @staticmethod
    def _convolveSame(image, kernel):
        """2D convolution via FFT (no scipy dependency), cropped to the
        same output size as the input, centered on the kernel.
        """
        padShape = (image.shape[0] + kernel.shape[0] - 1, image.shape[1] + kernel.shape[1] - 1)
        imageF = np.fft.rfft2(image, s=padShape)
        kernelF = np.fft.rfft2(kernel, s=padShape)
        convolved = np.fft.irfft2(imageF * kernelF, s=padShape)
        startRow = kernel.shape[0] // 2
        startCol = kernel.shape[1] // 2
        return convolved[startRow:startRow + image.shape[0], startCol:startCol + image.shape[1]]

    @staticmethod
    def _computeFanMask(sizeX, sizeY, spacingX, spacingY, viewAngleDeg, minRangeMm, maxRangeMm, depthOffsetMm=0.0):
        """Boolean array (shape sizeY x sizeX), True inside the fan-shaped
        field of view.

        The true apex (the catheter tip) is at depth 0 and is where image
        row j=0 is physically anchored (see _computeIJKToRAS), but for the
        purpose of shaping this mask only, row j=0 is treated as depth
        depthOffsetMm instead of depth 0 (see _computeDepthOffsetMm) so
        that when a near-range cutoff is in effect, the fan's straight
        edges (and the rounded cutout between them) touch the image's near
        edge instead of leaving a blank gap above them. Angles open
        symmetrically from the central (depth) axis within
        +/- viewAngleDeg/2; pixels whose radial distance from the true
        apex (using this same row-0-is-depthOffsetMm convention) falls
        outside [minRangeMm, maxRangeMm] are excluded as well.
        """
        cx = (sizeX - 1) / 2.0
        lateral = (np.arange(sizeX)[np.newaxis, :] - cx) * spacingX
        depth = depthOffsetMm + np.arange(sizeY)[:, np.newaxis] * spacingY
        angleDeg = np.degrees(np.arctan2(np.abs(lateral), depth))
        radius = np.sqrt(lateral ** 2 + depth ** 2)
        return (angleDeg <= (viewAngleDeg / 2.0)) & (radius >= minRangeMm) & (radius <= maxRangeMm)

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
        # Label map display nodes (if outputSegmentation is a
        # vtkMRMLLabelMapVolumeNode) have no window/level concept; only
        # apply this to scalar volume displays.
        if displayNode and hasattr(displayNode, "SetAutoWindowLevel"):
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
