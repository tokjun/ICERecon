cmake_minimum_required(VERSION 3.16.3...3.19.7 FATAL_ERROR)

project(ICEReconExtension)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_HOMEPAGE "https://github.com/tokjun/ICEReconExtension")
set(EXTENSION_CATEGORY "Ultrasound")
set(EXTENSION_CONTRIBUTORS "Junichi Tokuda (Brigham and Women's Hospital)")
set(EXTENSION_DESCRIPTION "This extension reconstructs a 3D model of the heart chamber from 2D intracardiac echo (ICE) images. It includes ICESim, which simulates 2D ICE imaging from a 3D segmentation, and ICERecon, which reconstructs a 3D model from a series of 2D ICE images.")
set(EXTENSION_ICONURL "")
set(EXTENSION_SCREENSHOTURLS "")
set(EXTENSION_DEPENDS "NA") # Specified as a list or "NA" if no dependencies

#-----------------------------------------------------------------------------
find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

#-----------------------------------------------------------------------------
add_subdirectory(ICESim)
add_subdirectory(ICERecon)

#-----------------------------------------------------------------------------
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
