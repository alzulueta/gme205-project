# ---------------------------------------------------------
# This now contains the object-oriented / OOAD-based classes.
# It uses ArcPy and ArcGIS Pro Spatial Analyst to process a Surface Model / DSM for rooftop solar PV suitability analysis.
# ---------------------------------------------------------

import os
import shutil
import arcpy
from arcpy.sa import ExtractByMask, Slope, Aspect, Con, Raster


class SpatialObject:
    """Base spatial object from the UML design."""

    def __init__(self, geometry=None):
        self.geometry = geometry

    def distance_to(self, other):
        if self.geometry is None or other.geometry is None:
            return None
        return self.geometry.distanceTo(other.geometry)


class StudyArea(SpatialObject):
    """
    UML connection:
    StudyArea represents the project boundary.
    In ArcPy, this is the KML/KMZ or polygon boundary used to clip the DSM.
    """

    def __init__(self, boundary_input):
        super().__init__()
        self.boundary_input = boundary_input
        self.boundary_fc = None

    def validate(self):
        if not self.boundary_input:
            raise ValueError("Boundary input is empty.")
        if not arcpy.Exists(self.boundary_input):
            raise FileNotFoundError(f"Boundary does not exist: {self.boundary_input}")

    def prepare_boundary(self, output_folder):
        """
        Accepts:
        - KML/KMZ boundary file; or
        - Polygon feature class / shapefile
        Returns a polygon feature class usable by ArcPy.
        """

        self.validate()
        boundary_lower = self.boundary_input.lower()

        if boundary_lower.endswith(".kml") or boundary_lower.endswith(".kmz"):
            arcpy.AddMessage("Converting KML/KMZ boundary to polygon...")

            temp_folder = os.path.join(output_folder, "temp_boundary")
            if os.path.exists(temp_folder):
                try:
                    shutil.rmtree(temp_folder)
                except Exception:
                    pass
            os.makedirs(temp_folder, exist_ok=True)

            arcpy.conversion.KMLToLayer(self.boundary_input, temp_folder, "Boundary_Layer")
            boundary_gdb = os.path.join(temp_folder, "Boundary_Layer.gdb")

            polygon_fc = self._find_polygon_feature_class(boundary_gdb)
            if polygon_fc is None:
                raise RuntimeError("No polygon feature was found inside the KML/KMZ boundary.")

            self.boundary_fc = polygon_fc
            return self.boundary_fc

        desc = arcpy.Describe(self.boundary_input)
        if hasattr(desc, "shapeType") and desc.shapeType.lower() == "polygon":
            self.boundary_fc = self.boundary_input
            return self.boundary_fc

        raise RuntimeError("Boundary input must be a KML/KMZ file or polygon feature class.")

    def _find_polygon_feature_class(self, geodatabase):
        old_workspace = arcpy.env.workspace
        try:
            arcpy.env.workspace = geodatabase

            for dataset in arcpy.ListDatasets("", "Feature") or []:
                dataset_path = os.path.join(geodatabase, dataset)
                arcpy.env.workspace = dataset_path
                polygons = arcpy.ListFeatureClasses("", "Polygon") or []
                if polygons:
                    return os.path.join(dataset_path, polygons[0])

            arcpy.env.workspace = geodatabase
            polygons = arcpy.ListFeatureClasses("", "Polygon") or []
            if polygons:
                return os.path.join(geodatabase, polygons[0])

            return None
        finally:
            arcpy.env.workspace = old_workspace


class SurfaceModel:
    """
    UML connection:
    SurfaceModel represents the DSM / Digital Surface Model.
    This object clips the DSM and derives slope and aspect.
    """

    def __init__(self, surface_model_raster):
        self.surface_model_raster = surface_model_raster
        self.clipped_surface_model = None
        self.slope_raster = None
        self.aspect_raster = None

    def validate(self):
        if not self.surface_model_raster:
            raise ValueError("Surface Model / DSM input is empty.")
        if not arcpy.Exists(self.surface_model_raster):
            raise FileNotFoundError(f"Surface Model / DSM does not exist: {self.surface_model_raster}")

    def clip_to_study_area(self, boundary_fc, output_folder):
        arcpy.AddMessage("Clipping Surface Model / DSM to study area...")
        self.validate()

        out_raster = os.path.join(output_folder, "surface_model_clip.tif")
        clipped = ExtractByMask(self.surface_model_raster, boundary_fc)
        clipped.save(out_raster)

        self.clipped_surface_model = out_raster
        return out_raster

    def derive_slope(self, output_folder):
        arcpy.AddMessage("Computing slope from Surface Model / DSM...")
        if self.clipped_surface_model is None:
            raise RuntimeError("Surface Model must be clipped before slope can be derived.")

        out_slope = os.path.join(output_folder, "slope_degrees.tif")
        slope = Slope(self.clipped_surface_model, "DEGREE")
        slope.save(out_slope)

        self.slope_raster = out_slope
        return out_slope

    def derive_aspect(self, output_folder):
        arcpy.AddMessage("Computing aspect from Surface Model / DSM...")
        if self.clipped_surface_model is None:
            raise RuntimeError("Surface Model must be clipped before aspect can be derived.")

        out_aspect = os.path.join(output_folder, "aspect_degrees.tif")
        aspect = Aspect(self.clipped_surface_model)
        aspect.save(out_aspect)

        self.aspect_raster = out_aspect
        return out_aspect


class Rooftop(SpatialObject):
    """
    UML connection:
    Rooftop is the conceptual object being evaluated.
    In this implementation, rooftop characteristics are represented
    by DSM-derived slope and aspect.
    """

    def __init__(self, geometry=None, slope=None, aspect=None):
        super().__init__(geometry)
        self.slope = slope
        self.aspect = aspect
        self.suitability_class = None

    def classify_suitability(self, criteria):
        self.suitability_class = criteria.evaluate_cell(self.slope, self.aspect)
        return self.suitability_class


class SuitabilityCriteria:
    """
    UML connection:
    SuitabilityCriteria stores the rules used to classify rooftop suitability.
    """

    def __init__(self, max_slope_east_west=6, max_slope_other=15):
        self.max_slope_east_west = max_slope_east_west
        self.max_slope_other = max_slope_other

    def evaluate_cell(self, slope_value, aspect_value):
        """Conceptual object-level evaluation for one rooftop/cell."""

        if slope_value is None or aspect_value is None:
            return "No Data"

        east = 67.5 <= aspect_value <= 112.5
        west = 247.5 <= aspect_value <= 292.5
        east_west = east or west

        if east_west and slope_value <= self.max_slope_east_west:
            return "Suitable"
        if not east_west and slope_value <= self.max_slope_other:
            return "Suitable"

        return "Not Suitable"

    def generate_suitability_raster(self, slope_raster, aspect_raster, output_folder):
        """
        Raster-based implementation of the suitability model.
        Output values:
        1 = Suitable
        0 = Not Suitable
        """

        arcpy.AddMessage("Applying slope-aspect suitability rules...")

        slope = Raster(slope_raster)
        aspect = Raster(aspect_raster)

        east = (aspect >= 67.5) & (aspect <= 112.5)
        west = (aspect >= 247.5) & (aspect <= 292.5)
        east_west = east | west

        other_aspects = (
            (aspect < 67.5) |
            ((aspect > 112.5) & (aspect < 247.5)) |
            (aspect > 292.5)
        )

        suitable_condition = (
            (east_west & (slope <= self.max_slope_east_west)) |
            (other_aspects & (slope <= self.max_slope_other))
        )

        out_suitability = os.path.join(output_folder, "suitability_raster.tif")
        suitability = Con(suitable_condition, 1, 0)
        suitability.save(out_suitability)

        return out_suitability


class SuitabilityResult:
    """
    UML connection:
    SuitabilityResult stores the final output of the suitability analysis.
    """

    def __init__(self, suitability_raster):
        self.suitability_raster = suitability_raster
        self.suitability_polygon = None
        self.usable_areas = None
        self.not_usable_areas = None

    def export_polygons(self, output_folder):
        arcpy.AddMessage("Converting suitability raster to polygon outputs...")

        self.suitability_polygon = os.path.join(output_folder, "suitability_polygon.shp")
        arcpy.conversion.RasterToPolygon(
            self.suitability_raster,
            self.suitability_polygon,
            "NO_SIMPLIFY",
            "VALUE"
        )

        self.usable_areas = os.path.join(output_folder, "usable_areas.shp")
        self.not_usable_areas = os.path.join(output_folder, "not_usable_areas.shp")

        arcpy.analysis.Select(self.suitability_polygon, self.usable_areas, '"gridcode" = 1')
        arcpy.analysis.Select(self.suitability_polygon, self.not_usable_areas, '"gridcode" = 0')

        self._add_area_field(self.usable_areas)
        self._add_area_field(self.not_usable_areas)

        return self.usable_areas, self.not_usable_areas

    def _add_area_field(self, feature_class):
        if not arcpy.Exists(feature_class):
            return

        field_names = [field.name for field in arcpy.ListFields(feature_class)]

        if "Area_sqm" not in field_names:
            arcpy.management.AddField(feature_class, "Area_sqm", "DOUBLE")

        arcpy.management.CalculateField(
            feature_class,
            "Area_sqm",
            "!shape.area@SQUAREMETERS!",
            "PYTHON3"
        )


class OutputManager:
    """
    UML connection:
    OutputManager manages output folder preparation and output reporting.
    """

    def __init__(self, output_folder):
        self.output_folder = output_folder

    def prepare(self):
        if not self.output_folder:
            raise ValueError("Output folder is empty.")
        if not os.path.exists(self.output_folder):
            os.makedirs(self.output_folder)

    def report(self, output_dictionary):
        arcpy.AddMessage("Generated outputs:")
        for name, path in output_dictionary.items():
            arcpy.AddMessage(f"  {name}: {path}")


class RooftopPVSuitabilityTool:
    """
    Main application object.
    This connects the UML objects into one complete computational workflow.
    """

    def __init__(self, boundary_input, surface_model_raster, output_folder):
        self.study_area = StudyArea(boundary_input)
        self.surface_model = SurfaceModel(surface_model_raster)
        self.criteria = SuitabilityCriteria()
        self.output_manager = OutputManager(output_folder)
        self.result = None

    def run(self):
        arcpy.AddMessage("=" * 70)
        arcpy.AddMessage("Starting Rooftop Solar PV Suitability Analysis")
        arcpy.AddMessage("=" * 70)

        self.output_manager.prepare()

        boundary_fc = self.study_area.prepare_boundary(self.output_manager.output_folder)

        clipped_surface = self.surface_model.clip_to_study_area(
            boundary_fc,
            self.output_manager.output_folder
        )

        slope = self.surface_model.derive_slope(self.output_manager.output_folder)
        aspect = self.surface_model.derive_aspect(self.output_manager.output_folder)

        suitability_raster = self.criteria.generate_suitability_raster(
            slope,
            aspect,
            self.output_manager.output_folder
        )

        self.result = SuitabilityResult(suitability_raster)
        usable, not_usable = self.result.export_polygons(self.output_manager.output_folder)

        outputs = {
            "Clipped Surface Model": clipped_surface,
            "Slope Raster": slope,
            "Aspect Raster": aspect,
            "Suitability Raster": suitability_raster,
            "Usable Areas": usable,
            "Not Usable Areas": not_usable
        }

        self.output_manager.report(outputs)

        arcpy.AddMessage("=" * 70)
        arcpy.AddMessage("Rooftop Solar PV Suitability Analysis completed successfully.")
        arcpy.AddMessage("=" * 70)

        return outputs
