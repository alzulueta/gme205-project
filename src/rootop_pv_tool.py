"""
FINAL IMPLEMENTATION

This script contains the operational ArcGIS Pro Script Tool
used to perform rooftop solar PV suitability analysis.

Execution Environment:
- ArcGIS Pro
- ArcPy
- Spatial Analyst

This script generated the outputs presented in the study.
"""

import arcpy
import os
import shutil
import traceback
import time
import gc
from arcpy.sa import *

arcpy.CheckOutExtension("Spatial")
arcpy.env.overwriteOutput = True

TOOL_TITLE = "Site Assessment Automation Tool"
TARGET_PCS_NAME = "PRS 1992 Philippines Zone III"

# ---------------------------------------------------------
# SUITABILITY CONFIGURATION
# ---------------------------------------------------------
MAX_SLOPE_EW = 6           # Max slope for East/West facing areas
MAX_SLOPE_OTHER = 15       # Max slope for other directions
ASPECT_EW_VALUES = (1, 2)  # 1 = East, 2 = West
ASPECT_OTHER_VALUE = 0     # 0 = Other directions (flat/non-EW)


# ---------------------------------------------------------
# LOGGING HELPERS
# ---------------------------------------------------------
def msg(text):
    arcpy.AddMessage(text)


def warn(text):
    arcpy.AddWarning(text)


def err(text):
    arcpy.AddError(text)


def banner(text):
    msg("=" * 72)
    msg(text)
    msg("=" * 72)


# ---------------------------------------------------------
# SPATIAL REFERENCE HELPERS
# ---------------------------------------------------------
def get_target_spatial_reference():
    sr = arcpy.SpatialReference(TARGET_PCS_NAME)
    if not sr or not sr.name or sr.name == "Unknown":
        raise Exception(
            f"Unable to load spatial reference '{TARGET_PCS_NAME}'. "
            "Please verify that this coordinate system exists in ArcGIS Pro."
        )
    return sr


def describe_sr(dataset):
    return arcpy.Describe(dataset).spatialReference


def is_unknown_sr(sr):
    return sr is None or not sr.name or sr.name.lower() == "unknown"


def same_sr(sr1, sr2):
    if is_unknown_sr(sr1) or is_unknown_sr(sr2):
        return False
    if getattr(sr1, "factoryCode", None) and getattr(sr2, "factoryCode", None):
        if sr1.factoryCode == sr2.factoryCode:
            return True
    return sr1.name == sr2.name


def log_spatial_reference(label, dataset):
    sr = describe_sr(dataset)
    sr_name = sr.name if sr and sr.name else "Unknown"
    msg(f"  {label} spatial reference: {sr_name}")


def get_transformation_if_needed(from_dataset, target_sr):
    from_sr = describe_sr(from_dataset)
    if is_unknown_sr(from_sr):
        raise Exception(f"Unknown spatial reference: {from_dataset}")
    if same_sr(from_sr, target_sr):
        return None
    try:
        transformations = arcpy.ListTransformations(from_sr, target_sr)
        if transformations:
            return transformations[0]
    except Exception:
        pass
    return None


# ---------------------------------------------------------
# WORKSPACE / PATH HELPERS
# ---------------------------------------------------------
def is_gdb_workspace(path):
    return str(path).lower().endswith(".gdb")


def validate_workspace(workspace):
    if not workspace:
        raise Exception("Output workspace is empty.")
    if not arcpy.Exists(workspace):
        raise Exception(f"Output workspace does not exist: {workspace}")


def validate_name(name, workspace):
    return arcpy.ValidateTableName(name, workspace)


def vector_output(workspace, name):
    valid_name = validate_name(name, workspace)
    if is_gdb_workspace(workspace):
        return os.path.join(workspace, valid_name)
    return os.path.join(workspace, f"{valid_name}.shp")


def raster_output(workspace, name):
    valid_name = validate_name(name, workspace)
    return os.path.join(workspace, valid_name)


def kmz_output(workspace, name):
    parent = workspace_parent_dir(workspace)
    return os.path.join(parent, f"{name}.kmz")


def workspace_parent_dir(workspace):
    return os.path.dirname(workspace) if is_gdb_workspace(workspace) else workspace


def release_arcgis_locks():
    """Release all ArcGIS workspace locks before attempting file deletion."""
    try:
        arcpy.env.workspace = None
    except Exception:
        pass
    try:
        arcpy.env.scratchWorkspace = None
    except Exception:
        pass
    try:
        arcpy.management.ClearWorkspaceCache()
    except Exception:
        pass
    gc.collect()
    time.sleep(1)


def force_delete_folder(folder_path, max_attempts=5):
    """
    Forcefully delete a folder, retrying on PermissionError.
    Handles locked GDB files by attempting to delete file by file first.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            shutil.rmtree(folder_path)
            return True
        except PermissionError as e:
            # Try to individually delete locked files before retrying rmtree
            for root, dirs, files in os.walk(folder_path, topdown=False):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    try:
                        os.chmod(fpath, 0o777)
                        os.remove(fpath)
                    except Exception:
                        pass
                for dname in dirs:
                    dpath = os.path.join(root, dname)
                    try:
                        os.rmdir(dpath)
                    except Exception:
                        pass
            if attempt < max_attempts:
                time.sleep(1.5)
                gc.collect()
                try:
                    arcpy.management.ClearWorkspaceCache()
                except Exception:
                    pass
            else:
                return False
        except Exception:
            return False
    return False


def prepare_temp_folder(workspace, folder_name="temp_site_assessment"):
    parent = workspace_parent_dir(workspace)
    temp_folder = os.path.join(parent, folder_name)
    if os.path.exists(temp_folder):
        release_arcgis_locks()
        deleted = force_delete_folder(temp_folder)
        if not deleted:
            # If we still can't delete, use a timestamped folder instead
            import datetime
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            folder_name = f"{folder_name}_{timestamp}"
            temp_folder = os.path.join(parent, folder_name)
            warn(f"  Could not remove previous temp folder. Using new folder: {temp_folder}")
    os.makedirs(temp_folder, exist_ok=True)
    return temp_folder


def create_scratch_gdb(temp_folder, gdb_name="scratch_processing.gdb"):
    gdb_path = os.path.join(temp_folder, gdb_name)
    if arcpy.Exists(gdb_path):
        arcpy.management.Delete(gdb_path)
    arcpy.management.CreateFileGDB(temp_folder, gdb_name)
    return gdb_path


# ---------------------------------------------------------
# VALIDATION HELPERS
# ---------------------------------------------------------
def validate_input_kml(input_kml):
    if not input_kml:
        raise Exception("Input KML/KMZ is empty.")
    if not arcpy.Exists(input_kml):
        raise Exception(f"Input KML/KMZ does not exist: {input_kml}")


def validate_dtm_list(dtm_input):
    """
    Accepts either:
    - A semicolon-separated list of raster file paths, OR
    - A single folder path (auto-scans recursively for all rasters inside)

    Supported raster formats: .bil, .tif, .tiff, .img, .dem, .asc, .flt
    """
    dtm_input = dtm_input.strip()

    SUPPORTED_EXTENSIONS = (".bil", ".tif", ".tiff", ".img", ".dem", ".asc", ".flt")

    # -------------------------------------------------------
    # CASE 1: Input is a folder — auto-scan for rasters
    # -------------------------------------------------------
    if os.path.isdir(dtm_input):
        msg(f"  DTM input is a folder. Scanning recursively: {dtm_input}")
        dtm_list = []

        for root, dirs, files in os.walk(dtm_input):
            for file in files:
                if file.lower().endswith(SUPPORTED_EXTENSIONS):
                    full_path = os.path.join(root, file)
                    dtm_list.append(full_path)

        if not dtm_list:
            err("=" * 72)
            err("ERROR: No raster files found in the specified folder.")
            err(f"Folder scanned : {dtm_input}")
            err(f"Supported formats: {', '.join(SUPPORTED_EXTENSIONS)}")
            err("Please ensure your DTM files are inside the selected folder.")
            err("=" * 72)
            raise Exception(f"No raster files found in folder: {dtm_input}")

        msg(f"  Found {len(dtm_list)} raster(s):")
        for r in dtm_list:
            msg(f"    - {os.path.basename(r)}")
        return dtm_list

    # -------------------------------------------------------
    # CASE 2: Input is a semicolon-separated list of paths
    # -------------------------------------------------------
    dtm_list = [p.strip().strip("'\"") for p in dtm_input.split(";") if p.strip()]

    if not dtm_list:
        raise Exception("No DTM raster inputs were provided.")

    missing = [p for p in dtm_list if not arcpy.Exists(p)]
    if missing:
        err("=" * 72)
        err("ERROR: One or more DTM raster files could not be found.")
        err("This usually happens when the tool is run on a different machine.")
        err("Missing files:")
        for m in missing:
            err(f"  - {m}")
        err("")
        err("SOLUTION: Re-browse to the correct DTM files on your machine,")
        err("or point the DTM input to a folder containing your raster files.")
        err("=" * 72)
        raise Exception(
            f"The following DTM rasters do not exist: {', '.join(missing)}"
        )

    msg(f"  Validated {len(dtm_list)} DTM raster(s):")
    for r in dtm_list:
        msg(f"    - {os.path.basename(r)}")
    return dtm_list


def get_boundary_output(output_workspace, boundary_param):
    if boundary_param:
        return boundary_param
    return vector_output(output_workspace, "Site_Boundary")


# ---------------------------------------------------------
# DATASET HELPERS
# ---------------------------------------------------------
def repair_geometry_if_vector(dataset):
    desc = arcpy.Describe(dataset)
    if hasattr(desc, "shapeType"):
        try:
            arcpy.management.RepairGeometry(dataset)
            msg(f"  Geometry repaired: {dataset}")
        except Exception:
            warn(f"  Geometry repair skipped or failed: {dataset}")


def add_area_hectares_field(feature_class, field_name="Area_ha"):
    existing_fields = [f.name for f in arcpy.ListFields(feature_class)]
    if field_name not in existing_fields:
        arcpy.management.AddField(feature_class, field_name, "DOUBLE")
    arcpy.management.CalculateField(
        feature_class,
        field_name,
        "!shape.area@HECTARES!",
        "PYTHON3"
    )


def get_field_names(feature_class):
    return [f.name.lower() for f in arcpy.ListFields(feature_class)]


def resolve_gridcode_field(feature_class):
    fields = get_field_names(feature_class)
    candidates = ["gridcode", "grid_code", "gridcod", "value", "classvalue"]
    for candidate in candidates:
        if candidate.lower() in fields:
            return candidate
    raise Exception(
        f"Could not find a grid/class field in {feature_class}. "
        f"Available fields: {', '.join(get_field_names(feature_class))}"
    )


def copy_features_clean(in_fc, out_fc):
    arcpy.management.CopyFeatures(in_fc, out_fc)
    repair_geometry_if_vector(out_fc)
    return out_fc


def copy_raster_clean(in_raster, out_raster):
    arcpy.management.CopyRaster(in_raster, out_raster)
    return out_raster


def create_empty_polygon(output_fc, template_fc):
    out_path = os.path.dirname(output_fc)
    out_name = os.path.basename(output_fc)
    spatial_ref = arcpy.Describe(template_fc).spatialReference
    arcpy.management.CreateFeatureclass(
        out_path,
        out_name,
        "POLYGON",
        spatial_reference=spatial_ref
    )
    repair_geometry_if_vector(output_fc)


# ---------------------------------------------------------
# PROJECTION HELPERS
# ---------------------------------------------------------
def project_feature_if_needed(in_fc, out_fc, target_sr):
    in_sr = describe_sr(in_fc)
    if is_unknown_sr(in_sr):
        raise Exception(f"Input feature has unknown spatial reference: {in_fc}")
    if same_sr(in_sr, target_sr):
        return copy_features_clean(in_fc, out_fc)
    transform = get_transformation_if_needed(in_fc, target_sr)
    if transform:
        arcpy.management.Project(in_fc, out_fc, target_sr, transform)
        msg(f"  Applied transformation: {transform}")
    else:
        arcpy.management.Project(in_fc, out_fc, target_sr)
    repair_geometry_if_vector(out_fc)
    return out_fc


def project_raster_if_needed(in_raster, out_raster, target_sr, resampling_type="BILINEAR"):
    in_sr = describe_sr(in_raster)
    if is_unknown_sr(in_sr):
        raise Exception(f"Input raster has unknown spatial reference: {in_raster}")
    if same_sr(in_sr, target_sr):
        return copy_raster_clean(in_raster, out_raster)
    transform = get_transformation_if_needed(in_raster, target_sr)
    if transform:
        arcpy.management.ProjectRaster(
            in_raster,
            out_raster,
            target_sr,
            resampling_type,
            geographic_transform=transform
        )
        msg(f"  Applied transformation: {transform}")
    else:
        arcpy.management.ProjectRaster(
            in_raster,
            out_raster,
            target_sr,
            resampling_type
        )
    return out_raster


# ---------------------------------------------------------
# KML / KMZ PROCESSING — POLYLINE & POLYGON SUPPORT
# ---------------------------------------------------------
def is_closed_polyline(geometry):
    """Check if a polyline geometry is closed (first point == last point)."""
    if geometry is None:
        return False
    for part in geometry:
        if part.count < 3:
            return False
        first = part[0]
        last = part[-1]
        if first is None or last is None:
            return False
        if abs(first.X - last.X) > 1e-6 or abs(first.Y - last.Y) > 1e-6:
            return False
    return True


def convert_polylines_to_polygons(polyline_fc, output_fc, scratch_gdb):
    """
    Converts closed polylines to polygons.
    Skips open polylines and warns the user.
    Returns output_fc path if any polygons were created, else None.
    """
    msg("  Checking polyline features for closed geometry...")

    spatial_ref = arcpy.Describe(polyline_fc).spatialReference
    out_path = os.path.dirname(output_fc)
    out_name = os.path.basename(output_fc)

    arcpy.management.CreateFeatureclass(
        out_path, out_name, "POLYGON", spatial_reference=spatial_ref
    )

    closed_count = 0
    open_count = 0

    with arcpy.da.SearchCursor(polyline_fc, ["SHAPE@"]) as s_cursor:
        with arcpy.da.InsertCursor(output_fc, ["SHAPE@"]) as i_cursor:
            for row in s_cursor:
                geom = row[0]
                if geom is None:
                    continue

                if is_closed_polyline(geom):
                    array = arcpy.Array()
                    for part in geom:
                        ring = arcpy.Array()
                        for pnt in part:
                            if pnt:
                                ring.append(pnt)
                        array.append(ring)

                    polygon = arcpy.Polygon(array, spatial_ref)
                    i_cursor.insertRow([polygon])
                    closed_count += 1
                else:
                    open_count += 1

    if open_count > 0:
        warn(f"  {open_count} open polyline(s) were skipped (cannot convert to polygon).")
    if closed_count > 0:
        msg(f"  {closed_count} closed polyline(s) successfully converted to polygon.")

    return output_fc if closed_count > 0 else None


def convert_kml_to_boundary(input_kml, temp_folder):
    msg("Step 1/22: Converting KML/KMZ to layer...")
    arcpy.conversion.KMLToLayer(input_kml, temp_folder, "Site_Boundary")

    boundary_gdb = os.path.join(temp_folder, "Site_Boundary.gdb")

    msg("Validating KMZ content...")

    boundary_fc = None
    polyline_fc = None
    old_workspace = arcpy.env.workspace
    arcpy.env.workspace = boundary_gdb

    try:
        raster_list = arcpy.ListRasters()
        if raster_list:
            err("=" * 72)
            err("ERROR: The input KMZ file contains raster data.")
            err("This tool only processes vector polygon/polyline data.")
            err(f"Raster datasets found: {', '.join(raster_list)}")
            err("Please provide a KMZ file with polygon or closed polyline features.")
            err("=" * 72)
            raise Exception("Raster KMZ detected - processing stopped.")

        for fc in arcpy.ListFeatureClasses("", "Polygon") or []:
            boundary_fc = os.path.join(boundary_gdb, fc)
            break

        if not boundary_fc:
            for fc in arcpy.ListFeatureClasses("", "Polyline") or []:
                polyline_fc = os.path.join(boundary_gdb, fc)
                break

        if not boundary_fc and not polyline_fc:
            for fds in arcpy.ListDatasets("", "Feature") or []:
                arcpy.env.workspace = os.path.join(boundary_gdb, fds)

                for fc in arcpy.ListFeatureClasses("", "Polygon") or []:
                    boundary_fc = os.path.join(boundary_gdb, fds, fc)
                    break

                if not boundary_fc:
                    for fc in arcpy.ListFeatureClasses("", "Polyline") or []:
                        polyline_fc = os.path.join(boundary_gdb, fds, fc)
                        break

                arcpy.env.workspace = boundary_gdb

                if boundary_fc or polyline_fc:
                    break

    finally:
        arcpy.env.workspace = old_workspace

    if not boundary_fc and polyline_fc:
        msg("  No polygon found. Polyline detected — attempting closed polyline conversion...")
        converted_fc = os.path.join(boundary_gdb, "Converted_Polygon")
        result = convert_polylines_to_polygons(polyline_fc, converted_fc, boundary_gdb)

        if result is None:
            err("=" * 72)
            err("ERROR: Polyline features found but none are closed.")
            err("Only closed polylines can be converted to polygons.")
            err("Please provide a KMZ with polygon or closed polyline features.")
            err("=" * 72)
            raise Exception("No closed polylines found — cannot convert to polygon.")

        boundary_fc = result
        msg(f"  Polyline successfully converted to polygon: {boundary_fc}")

    if not boundary_fc or not arcpy.Exists(boundary_fc):
        err("=" * 72)
        err("ERROR: No polygon or convertible polyline features found in the KMZ file.")
        err(f"GDB searched: {boundary_gdb}")
        err("Please ensure your KMZ contains polygon or closed polyline boundaries.")
        err("=" * 72)
        raise Exception("No polygon features found in KMZ.")

    feature_count = int(arcpy.management.GetCount(boundary_fc)[0])
    if feature_count == 0:
        err("=" * 72)
        err("ERROR: The polygon feature class is empty.")
        err("Please provide a KMZ with at least one polygon or closed polyline feature.")
        err("=" * 72)
        raise Exception("Empty polygon feature class.")

    msg(f"  Valid polygon boundary detected ({feature_count} features)")
    msg(f"  Boundary extracted from: {boundary_fc}")

    return boundary_fc


# ---------------------------------------------------------
# DTM PROCESSING
# ---------------------------------------------------------
def project_dtms_to_target(dtm_list, scratch_gdb, target_sr):
    msg("Step 2/22: Projecting DTM rasters to PRS 1992 Philippines Zone III...")
    projected_dtms = []
    for idx, dtm in enumerate(dtm_list, start=1):
        out_name = validate_name(f"dtm_prj_{idx}", scratch_gdb)
        out_raster = os.path.join(scratch_gdb, out_name)
        project_raster_if_needed(dtm, out_raster, target_sr, resampling_type="BILINEAR")
        projected_dtms.append(out_raster)
        msg(f"  Projected DTM {idx}: {os.path.basename(dtm)}")
    return projected_dtms


def mosaic_dtms(dtm_list, scratch_gdb):
    msg("Step 3/22: Mosaicking projected DTM rasters...")
    mosaic_name = validate_name("Mosaic_DTM", scratch_gdb)
    mosaic_raster = os.path.join(scratch_gdb, mosaic_name)
    arcpy.management.MosaicToNewRaster(
        dtm_list,
        scratch_gdb,
        mosaic_name,
        pixel_type="32_BIT_FLOAT",
        number_of_bands=1
    )
    msg(f"  Mosaic raster: {mosaic_raster}")
    return mosaic_raster


# ---------------------------------------------------------
# BOUNDARY PROCESSING
# ---------------------------------------------------------
def export_and_project_boundary(boundary_fc_kml, boundary_output, scratch_gdb, target_sr):
    msg("Step 4/22: Projecting boundary to PRS 1992 Philippines Zone III...")
    boundary_projected_temp = os.path.join(
        scratch_gdb,
        validate_name("Boundary_PRS92_Zone3", scratch_gdb)
    )
    project_feature_if_needed(boundary_fc_kml, boundary_projected_temp, target_sr)
    copy_features_clean(boundary_projected_temp, boundary_output)
    msg(f"  Boundary output: {boundary_output}")
    return boundary_output


# ---------------------------------------------------------
# TERRAIN ANALYSIS
# ---------------------------------------------------------
def extract_aoi(mosaic_raster, boundary_fc, scratch_gdb):
    msg("Step 5/22: Extracting AOI from mosaic...")
    out_aoi = os.path.join(scratch_gdb, validate_name("AOI_Extract", scratch_gdb))
    extracted = ExtractByMask(mosaic_raster, boundary_fc)
    extracted.save(out_aoi)
    msg(f"  AOI raster: {out_aoi}")
    return out_aoi


def calculate_slope(aoi_raster, scratch_gdb):
    msg("Step 6/22: Calculating slope...")
    out_slope = os.path.join(scratch_gdb, validate_name("Slope_rast", scratch_gdb))
    slope = Slope(aoi_raster, "DEGREE", 1)
    slope.save(out_slope)
    msg(f"  Slope raster: {out_slope}")
    return out_slope


def reclassify_slope(slope_raster, scratch_gdb):
    msg("Step 7/22: Reclassifying slope...")
    out_reclass = os.path.join(scratch_gdb, validate_name("Slope_Class", scratch_gdb))
    slope_remap = RemapRange([
        [0, 3, 3],
        [3, 6, 6],
        [6, 10, 10],
        [10, 15, 15],
        [15, 20, 20],
        [20, 25, 25],
        [25, 30, 30],
        [30, 90, 90]
    ])
    Reclassify(slope_raster, "VALUE", slope_remap).save(out_reclass)
    msg(f"  Reclassified slope raster: {out_reclass}")
    return out_reclass


def slope_polygon_and_dissolve(reclass_slope, scratch_gdb):
    msg("Step 8/22: Converting slope raster to polygon...")
    slope_polygon = os.path.join(scratch_gdb, validate_name("Slope_polygon", scratch_gdb))
    arcpy.conversion.RasterToPolygon(reclass_slope, slope_polygon, "NO_SIMPLIFY", "VALUE")
    repair_geometry_if_vector(slope_polygon)
    msg(f"  Slope polygon: {slope_polygon}")

    msg("Step 9/22: Dissolving slope polygon...")
    slope_dissolved = os.path.join(scratch_gdb, validate_name("Slope_Dissolve", scratch_gdb))
    slope_field = resolve_gridcode_field(slope_polygon)
    arcpy.management.Dissolve(slope_polygon, slope_dissolved, slope_field)
    repair_geometry_if_vector(slope_dissolved)
    add_area_hectares_field(slope_dissolved, "Area_ha")
    msg(f"  Slope dissolved: {slope_dissolved}")

    return slope_polygon, slope_dissolved


def calculate_aspect(aoi_raster, scratch_gdb):
    msg("Step 10/22: Calculating aspect...")
    out_aspect = os.path.join(scratch_gdb, validate_name("Aspect_rast", scratch_gdb))
    aspect = Aspect(aoi_raster)
    aspect.save(out_aspect)
    msg(f"  Aspect raster: {out_aspect}")
    return out_aspect


def reclassify_aspect(aspect_raster, scratch_gdb):
    msg("Step 11/22: Reclassifying aspect...")
    out_reclass = os.path.join(scratch_gdb, validate_name("Aspect_Class", scratch_gdb))
    aspect_remap = RemapRange([
        [-1, 0, 0],
        [0, 22.5, 0],
        [22.5, 67.5, 0],
        [67.5, 112.5, 1],
        [112.5, 157.5, 0],
        [157.5, 202.5, 0],
        [202.5, 247.5, 0],
        [247.5, 292.5, 2],
        [292.5, 337.5, 0],
        [337.5, 360, 0]
    ])
    Reclassify(aspect_raster, "VALUE", aspect_remap).save(out_reclass)
    msg(f"  Reclassified aspect raster: {out_reclass}")
    return out_reclass


def aspect_polygon_and_dissolve(reclass_aspect, scratch_gdb):
    msg("Step 12/22: Converting aspect raster to polygon...")
    aspect_polygon = os.path.join(scratch_gdb, validate_name("Aspect_polygon", scratch_gdb))
    arcpy.conversion.RasterToPolygon(reclass_aspect, aspect_polygon, "NO_SIMPLIFY", "VALUE")
    repair_geometry_if_vector(aspect_polygon)
    msg(f"  Aspect polygon: {aspect_polygon}")

    msg("Step 13/22: Dissolving aspect polygon...")
    aspect_dissolved = os.path.join(scratch_gdb, validate_name("Aspect_Dissolve", scratch_gdb))
    aspect_field = resolve_gridcode_field(aspect_polygon)
    arcpy.management.Dissolve(aspect_polygon, aspect_dissolved, aspect_field)
    repair_geometry_if_vector(aspect_dissolved)
    msg(f"  Aspect dissolved: {aspect_dissolved}")

    return aspect_polygon, aspect_dissolved


# ---------------------------------------------------------
# SUITABILITY ANALYSIS
# ---------------------------------------------------------
def run_suitability_analysis(slope_dissolved, aspect_dissolved, boundary_fc, scratch_gdb):
    """
    Step 6 (table): Intersect Slope 0-6  + Aspect EW (grid 1-2)  → Areas w 0-6, EW
    Step 7 (table): Intersect Slope 0-15 + Aspect Other (grid 0) → Areas w 0-15, Other
    Step 8 (table): Merge both results                            → Usable Area
    Step 9 (table): Erase Usable from Site Boundary              → Not Usable Area
    """
    slope_field = resolve_gridcode_field(slope_dissolved)
    aspect_field = resolve_gridcode_field(aspect_dissolved)
    msg(f"  Slope class field  : {slope_field}")
    msg(f"  Aspect class field : {aspect_field}")

    aspect_ew_sql = ", ".join(str(v) for v in ASPECT_EW_VALUES)

    # --- Step 6: Slope ≤ 6 intersect with Aspect EW ---
    msg("Step 10/22: Selecting slope ≤ 6 (EW suitability)...")
    slope_0_6 = os.path.join(scratch_gdb, validate_name("Slope_0_6", scratch_gdb))
    arcpy.analysis.Select(slope_dissolved, slope_0_6, f'"{slope_field}" <= {MAX_SLOPE_EW}')
    repair_geometry_if_vector(slope_0_6)

    msg("Step 11/22: Selecting aspect EW (grid code 1-2)...")
    aspect_ew = os.path.join(scratch_gdb, validate_name("Aspect_EW", scratch_gdb))
    arcpy.analysis.Select(aspect_dissolved, aspect_ew, f'"{aspect_field}" IN ({aspect_ew_sql})')
    repair_geometry_if_vector(aspect_ew)

    msg("Step 12/22: Intersecting slope 0-6 with aspect EW → Areas w 0-6, EW...")
    areas_0_6_ew = os.path.join(scratch_gdb, validate_name("Areas_0_6_EW", scratch_gdb))
    arcpy.analysis.Intersect([slope_0_6, aspect_ew], areas_0_6_ew)
    repair_geometry_if_vector(areas_0_6_ew)
    count_ew = int(arcpy.management.GetCount(areas_0_6_ew)[0])
    msg(f"  Areas w 0-6 EW: {count_ew} features")

    # --- Step 7: Slope ≤ 15 intersect with Aspect Other ---
    msg("Step 13/22: Selecting slope ≤ 15 (Other directions suitability)...")
    slope_0_15 = os.path.join(scratch_gdb, validate_name("Slope_0_15", scratch_gdb))
    arcpy.analysis.Select(slope_dissolved, slope_0_15, f'"{slope_field}" <= {MAX_SLOPE_OTHER}')
    repair_geometry_if_vector(slope_0_15)

    msg("Step 14/22: Selecting aspect Other directions (grid code 0)...")
    aspect_other = os.path.join(scratch_gdb, validate_name("Aspect_Other", scratch_gdb))
    arcpy.analysis.Select(aspect_dissolved, aspect_other, f'"{aspect_field}" = {ASPECT_OTHER_VALUE}')
    repair_geometry_if_vector(aspect_other)

    msg("Step 15/22: Intersecting slope 0-15 with aspect Other → Areas w 0-15, Other directions...")
    areas_0_15_other = os.path.join(scratch_gdb, validate_name("Areas_0_15_Other", scratch_gdb))
    arcpy.analysis.Intersect([slope_0_15, aspect_other], areas_0_15_other)
    repair_geometry_if_vector(areas_0_15_other)
    count_other = int(arcpy.management.GetCount(areas_0_15_other)[0])
    msg(f"  Areas w 0-15 Other: {count_other} features")

    # --- Step 8: Merge both results → Usable Area ---
    msg("Step 16/22: Merging EW and Other areas → Usable Area...")
    usable_scratch = os.path.join(scratch_gdb, validate_name("Usable_Area", scratch_gdb))

    if count_ew > 0 or count_other > 0:
        merge_inputs = []
        if count_ew > 0:
            merge_inputs.append(areas_0_6_ew)
        if count_other > 0:
            merge_inputs.append(areas_0_15_other)
        arcpy.management.Merge(merge_inputs, usable_scratch)
        repair_geometry_if_vector(usable_scratch)
        usable_count = int(arcpy.management.GetCount(usable_scratch)[0])
        msg(f"  Usable Area: {usable_count} features after merge")
    else:
        create_empty_polygon(usable_scratch, boundary_fc)
        usable_count = 0
        warn("  No usable areas found after merge. Entire site classified as not usable.")

    # --- Step 9: Erase Usable from Boundary → Not Usable Area ---
    msg("Step 17/22: Erasing usable area from boundary → Not Usable Area...")
    not_usable_scratch = os.path.join(scratch_gdb, validate_name("NotUsable_Area", scratch_gdb))
    if usable_count > 0:
        arcpy.analysis.Erase(boundary_fc, usable_scratch, not_usable_scratch)
        repair_geometry_if_vector(not_usable_scratch)
    else:
        copy_features_clean(boundary_fc, not_usable_scratch)

    not_usable_count = int(arcpy.management.GetCount(not_usable_scratch)[0])
    msg(f"  Not Usable Area: {not_usable_count} features")

    return usable_scratch, not_usable_scratch, usable_count


# ---------------------------------------------------------
# KMZ EXPORT HELPERS
# ---------------------------------------------------------
def export_fc_to_kmz(in_fc, kmz_file, temp_folder, layer_name):
    msg(f"  Exporting KMZ: {os.path.basename(kmz_file)}")

    layer_file = os.path.join(temp_folder, f"{layer_name}.lyrx")

    if arcpy.Exists(kmz_file):
        arcpy.management.Delete(kmz_file)
    if os.path.exists(layer_file):
        os.remove(layer_file)

    feature_layer = f"{layer_name}_lyr"
    arcpy.management.MakeFeatureLayer(in_fc, feature_layer)
    arcpy.management.SaveToLayerFile(feature_layer, layer_file, "RELATIVE")
    arcpy.conversion.LayerToKML(layer_file, kmz_file)

    try:
        arcpy.management.Delete(feature_layer)
    except Exception:
        pass


def export_final_kmz(out_slope_fc, out_usable, out_not_usable, output_workspace, temp_folder):
    msg("Step 19/22: Exporting KMZ outputs...")

    slope_kmz      = kmz_output(output_workspace, "Slope_Dissolve")
    usable_kmz     = kmz_output(output_workspace, "Usable_Areas")
    not_usable_kmz = kmz_output(output_workspace, "NotUsable_Areas")

    export_fc_to_kmz(out_slope_fc,   slope_kmz,      temp_folder, "Slope_Dissolve")
    export_fc_to_kmz(out_usable,     usable_kmz,     temp_folder, "Usable_Areas")
    export_fc_to_kmz(out_not_usable, not_usable_kmz, temp_folder, "NotUsable_Areas")

    return slope_kmz, usable_kmz, not_usable_kmz


# ---------------------------------------------------------
# FINAL OUTPUT EXPORT
# ---------------------------------------------------------
def export_final_rasters(aoi_raster, slope_raster, aspect_raster, output_workspace):
    msg("Step 18/22: Exporting final rasters to output workspace...")

    out_aoi    = raster_output(output_workspace, "AOI_Extract")
    out_slope  = raster_output(output_workspace, "Slope_rast")
    out_aspect = raster_output(output_workspace, "Aspect_rast")

    copy_raster_clean(aoi_raster,    out_aoi)
    copy_raster_clean(slope_raster,  out_slope)
    copy_raster_clean(aspect_raster, out_aspect)

    return out_aoi, out_slope, out_aspect


def export_final_vectors(boundary_fc, slope_dissolved, aspect_dissolved,
                         usable_scratch, not_usable_scratch, usable_count,
                         output_workspace):
    msg("Step 18/22: Exporting final vector outputs...")

    out_boundary   = vector_output(output_workspace, "Site_Boundary")
    out_slope      = vector_output(output_workspace, "Slope_Dissolve")
    out_aspect     = vector_output(output_workspace, "Aspect_Dissolve")
    out_usable     = vector_output(output_workspace, "Usable_Areas")
    out_not_usable = vector_output(output_workspace, "NotUsable_Areas")

    copy_features_clean(boundary_fc,        out_boundary)
    copy_features_clean(slope_dissolved,    out_slope)
    copy_features_clean(aspect_dissolved,   out_aspect)
    copy_features_clean(usable_scratch,     out_usable)
    copy_features_clean(not_usable_scratch, out_not_usable)

    add_area_hectares_field(out_slope,      "Area_ha")
    add_area_hectares_field(out_usable,     "Area_ha")
    add_area_hectares_field(out_not_usable, "Area_ha")

    return out_boundary, out_slope, out_aspect, out_usable, out_not_usable


# ---------------------------------------------------------
# TOOL OUTPUT PARAMETERS
# ---------------------------------------------------------
def set_output_parameters(boundary_fc, usable_fc, not_usable_fc,
                           slope_fc, aspect_fc,
                           aoi_raster, slope_raster, aspect_raster):
    arcpy.SetParameterAsText(3, boundary_fc)
    arcpy.SetParameterAsText(4, usable_fc)
    arcpy.SetParameterAsText(5, not_usable_fc)
    arcpy.SetParameterAsText(6, slope_fc)
    arcpy.SetParameterAsText(7, aspect_fc)
    arcpy.SetParameterAsText(8, aoi_raster)
    arcpy.SetParameterAsText(9, slope_raster)
    arcpy.SetParameterAsText(10, aspect_raster)


# ---------------------------------------------------------
# CLEANUP
# ---------------------------------------------------------
def safe_cleanup_temp_folder(temp_folder):
    if not temp_folder or not os.path.exists(temp_folder):
        return

    try:
        arcpy.env.workspace = None
    except Exception:
        pass

    try:
        arcpy.env.scratchWorkspace = None
    except Exception:
        pass

    try:
        arcpy.management.ClearWorkspaceCache()
    except Exception:
        pass

    gc.collect()
    time.sleep(1)

    for attempt in range(1, 6):
        try:
            shutil.rmtree(temp_folder)
            msg("Temporary files cleaned up.")
            return
        except Exception as cleanup_error:
            if attempt < 5:
                warn(f"Cleanup retry {attempt}/5 for temp folder...")
                time.sleep(1.5)
                gc.collect()
                try:
                    arcpy.management.ClearWorkspaceCache()
                except Exception:
                    pass
            else:
                warn(f"Could not remove temp folder: {temp_folder}")
                warn(f"Cleanup error details: {cleanup_error}")


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------
def SiteAssessement():
    banner(f"Starting {TOOL_TITLE}")

    temp_folder = None
    scratch_gdb = None

    extracted  = None
    slope_obj  = None
    aspect_obj = None

    try:
        msg("Step 1/22: Reading and validating inputs...")
        input_kml        = arcpy.GetParameterAsText(0)
        dtm_input        = arcpy.GetParameterAsText(1)   # Folder OR semicolon list
        output_workspace = arcpy.GetParameterAsText(2)
        boundary_param   = arcpy.GetParameterAsText(3)

        validate_input_kml(input_kml)
        validate_workspace(output_workspace)

        # --- DTM: accepts folder or semicolon-separated list ---
        msg("  Resolving DTM inputs...")
        dtm_list = validate_dtm_list(dtm_input)
        msg(f"  Total DTM rasters to process: {len(dtm_list)}")

        target_sr = get_target_spatial_reference()
        msg(f"  Target projection: {target_sr.name}")

        boundary_output = get_boundary_output(output_workspace, boundary_param)

        msg("Step 2/22: Preparing temporary workspace...")
        temp_folder = prepare_temp_folder(output_workspace)
        scratch_gdb = create_scratch_gdb(temp_folder)
        arcpy.env.workspace        = scratch_gdb
        arcpy.env.scratchWorkspace = scratch_gdb
        msg(f"  Temporary folder    : {temp_folder}")
        msg(f"  Scratch geodatabase : {scratch_gdb}")

        msg("Step 3/22: Extracting polygon boundary from KMZ/KML...")
        boundary_fc_kml = convert_kml_to_boundary(input_kml, temp_folder)

        msg("Step 4/22: Preparing DTM rasters...")
        projected_dtms = project_dtms_to_target(dtm_list, scratch_gdb, target_sr)

        msg("Step 5/22: Creating DTM mosaic...")
        mosaic_raster = mosaic_dtms(projected_dtms, scratch_gdb)

        msg("Step 6/22: Projecting boundary...")
        boundary_fc = export_and_project_boundary(
            boundary_fc_kml, boundary_output, scratch_gdb, target_sr
        )

        msg("Step 7/22: Extracting AOI...")
        aoi_raster = extract_aoi(mosaic_raster, boundary_fc, scratch_gdb)

        msg("Step 8/22: Generating slope surfaces...")
        slope_raster  = calculate_slope(aoi_raster, scratch_gdb)
        reclass_slope = reclassify_slope(slope_raster, scratch_gdb)
        slope_polygon, slope_dissolved = slope_polygon_and_dissolve(reclass_slope, scratch_gdb)

        msg("Step 9/22: Generating aspect surfaces...")
        aspect_raster  = calculate_aspect(aoi_raster, scratch_gdb)
        reclass_aspect = reclassify_aspect(aspect_raster, scratch_gdb)
        aspect_polygon, aspect_dissolved = aspect_polygon_and_dissolve(reclass_aspect, scratch_gdb)

        # --- Suitability Analysis (Steps 10–17) ---
        usable_scratch, not_usable_scratch, usable_count = run_suitability_analysis(
            slope_dissolved, aspect_dissolved, boundary_fc, scratch_gdb
        )

        # --- Export ---
        out_aoi, out_slope_raster, out_aspect_raster = export_final_rasters(
            aoi_raster, slope_raster, aspect_raster, output_workspace
        )

        out_boundary, out_slope_fc, out_aspect_fc, out_usable, out_not_usable = export_final_vectors(
            boundary_fc, slope_dissolved, aspect_dissolved,
            usable_scratch, not_usable_scratch, usable_count,
            output_workspace
        )

        msg("Step 20/22: Verifying spatial references...")
        log_spatial_reference("Boundary",         out_boundary)
        log_spatial_reference("AOI Raster",       out_aoi)
        log_spatial_reference("Slope Raster",     out_slope_raster)
        log_spatial_reference("Aspect Raster",    out_aspect_raster)
        log_spatial_reference("Slope Dissolve",   out_slope_fc)
        log_spatial_reference("Aspect Dissolve",  out_aspect_fc)
        log_spatial_reference("Usable Areas",     out_usable)
        log_spatial_reference("Not Usable Areas", out_not_usable)

        msg("Step 21/22: Publishing tool outputs...")
        set_output_parameters(
            out_boundary,
            out_usable,
            out_not_usable,
            out_slope_fc,
            out_aspect_fc,
            out_aoi,
            out_slope_raster,
            out_aspect_raster
        )

        slope_kmz, usable_kmz, not_usable_kmz = export_final_kmz(
            out_slope_fc, out_usable, out_not_usable,
            output_workspace, temp_folder
        )

        msg(f"  Slope KMZ        : {slope_kmz}")
        msg(f"  Usable Areas KMZ : {usable_kmz}")
        msg(f"  Not Usable KMZ   : {not_usable_kmz}")

        if usable_count == 0:
            warn("WARNING: No usable areas found. Consider adjusting slope/aspect criteria.")

        msg("Step 22/22: Completing process...")
        banner("Site Assessment Completed Successfully")
        msg(f"Output files saved to: {output_workspace}")

    except Exception as e:
        err(f"Processing failed: {str(e)}")
        err(traceback.format_exc())
        raise

    finally:
        extracted  = None
        slope_obj  = None
        aspect_obj = None

        try:
            del extracted
        except Exception:
            pass
        try:
            del slope_obj
        except Exception:
            pass
        try:
            del aspect_obj
        except Exception:
            pass

        gc.collect()
        safe_cleanup_temp_folder(temp_folder)


# ---------------------------------------------------------
# EXECUTE
# ---------------------------------------------------------
if __name__ == "__main__":
    SiteAssessement()
