# ---------------------------------------------------------
# This connect to the ArcGIS Pro Script Tool.
# It imports the OOAD/OOP classes from rooftop_pv_tool.py.
#
# ArcGIS Pro Script Tool Parameters:
#   0 - Input Boundary KML/KMZ or Polygon Feature
#   1 - Input Surface Model / DSM Raster
#   2 - Output Folder
# ---------------------------------------------------------

import traceback
import arcpy
from rooftop_pv_tool import RooftopPVSuitabilityTool


def main():
    try:
        # These parameters come from the ArcGIS Pro Script Tool interface.
        boundary_input = arcpy.GetParameterAsText(0)
        surface_model = arcpy.GetParameterAsText(1)
        output_folder = arcpy.GetParameterAsText(2)

        # This object connects the UML design to the actual ArcPy workflow.
        tool = RooftopPVSuitabilityTool(
            boundary_input=boundary_input,
            surface_model_raster=surface_model,
            output_folder=output_folder
        )

        outputs = tool.run()

        # Optional: Set derived outputs only if parameters are configured.
        # If not configured, the tool still works because outputs are saved to folder.
        try:
            arcpy.SetParameterAsText(3, outputs["Slope Raster"])
            arcpy.SetParameterAsText(4, outputs["Aspect Raster"])
            arcpy.SetParameterAsText(5, outputs["Usable Areas"])
            arcpy.SetParameterAsText(6, outputs["Not Usable Areas"])
        except Exception:
            arcpy.AddWarning("Derived output parameters were not configured, but output files were saved successfully.")

    except Exception as exc:
        arcpy.AddError("Rooftop PV suitability tool failed.")
        arcpy.AddError(str(exc))
        arcpy.AddError(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
