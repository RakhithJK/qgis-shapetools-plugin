import os
from geographiclib.geodesic import Geodesic

from qgis.core import (
    QgsPointXY, QgsGeometry, QgsField,
    QgsProject, QgsWkbTypes, QgsCoordinateTransform, QgsPropertyDefinition)

from qgis.core import (
    QgsProcessing,
    QgsProcessingParameters,
    QgsProcessingFeatureBasedAlgorithm,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterNumber,
    QgsProcessingParameterEnum)

from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import QVariant, QUrl

from .settings import settings, epsg4326, geod
from .utils import tr, conversionToMeters, DISTANCE_LABELS, makeIdlCrossingsPositive, hasIdlCrossing

SHAPE_TYPE = [tr("Polygon"), tr("Line")]


class CreateDonutAlgorithm(QgsProcessingFeatureBasedAlgorithm):
    """
    Algorithm to create a donut shape.
    """

    PrmShapeType = 'ShapeType'
    PrmOuterRadius = 'OuterRadius'
    PrmInnerRadius = 'InnerRadius'
    PrmUnitsOfMeasure = 'UnitsOfMeasure'
    PrmDrawingSegments = 'DrawingSegments'
    PrmExportInputGeometry = 'ExportInputGeometry'

    def createInstance(self):
        return CreateDonutAlgorithm()

    def name(self):
        return 'createdonut'

    def icon(self):
        return QIcon(os.path.join(os.path.dirname(__file__), 'images/donut.png'))

    def displayName(self):
        return tr('Create donut')

    def group(self):
        return tr('Geodesic vector creation')

    def groupId(self):
        return 'vectorcreation'

    def outputName(self):
        return tr('Output layer')

    def helpUrl(self):
        file = os.path.dirname(__file__) + '/index.html'
        if not os.path.exists(file):
            return ''
        return QUrl.fromLocalFile(file).toString(QUrl.FullyEncoded)

    def inputLayerTypes(self):
        return [QgsProcessing.TypeVectorPoint]

    def outputWkbType(self, input_wkb_type):
        if self.shape_type == 0:
            return (QgsWkbTypes.Polygon)
        return (QgsWkbTypes.LineString)

    def outputFields(self, input_fields):
        if self.export_geom:
            name_x, name_y = settings.getGeomNames(input_fields.names())
            input_fields.append(QgsField(name_x, QVariant.Double))
            input_fields.append(QgsField(name_y, QVariant.Double))
        return(input_fields)

    def  supportInPlaceEdit(self, layer):
        return False

    def initParameters(self, config=None):
        self.shape_type = 0
        self.export_geom = False
        self.addParameter(
            QgsProcessingParameterEnum(
                self.PrmShapeType,
                tr('Shape type'),
                options=SHAPE_TYPE,
                defaultValue=0,
                optional=False)
        )
        param = QgsProcessingParameterNumber(
            self.PrmOuterRadius,
            tr('Outer radius'),
            QgsProcessingParameterNumber.Double,
            defaultValue=20.0,
            minValue=0,
            optional=False)
        param.setIsDynamic(True)
        param.setDynamicPropertyDefinition(QgsPropertyDefinition(
            self.PrmOuterRadius,
            tr('Outer radius'),
            QgsPropertyDefinition.Double))
        param.setDynamicLayerParameterName('INPUT')
        self.addParameter(param)

        param = QgsProcessingParameterNumber(
            self.PrmInnerRadius,
            tr('Inner radius'),
            QgsProcessingParameterNumber.Double,
            defaultValue=10.0,
            minValue=0,
            optional=False)
        param.setIsDynamic(True)
        param.setDynamicPropertyDefinition(QgsPropertyDefinition(
            self.PrmInnerRadius,
            tr('Inner radius'),
            QgsPropertyDefinition.Double))
        param.setDynamicLayerParameterName('INPUT')
        self.addParameter(param)

        self.addParameter(
            QgsProcessingParameterEnum(
                self.PrmUnitsOfMeasure,
                tr('Radius units'),
                options=DISTANCE_LABELS,
                defaultValue=0,
                optional=False)
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.PrmDrawingSegments,
                tr('Number of drawing segments'),
                QgsProcessingParameterNumber.Integer,
                defaultValue=36,
                minValue=4,
                optional=True)
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.PrmExportInputGeometry,
                tr('Add input geometry fields to output table'),
                False,
                optional=True)
        )

    def prepareAlgorithm(self, parameters, context, feedback):
        self.shape_type = self.parameterAsInt(parameters, self.PrmShapeType, context)
        self.outer_radius = self.parameterAsDouble(parameters, self.PrmOuterRadius, context)
        if self.outer_radius <= 0:
            feedback.reportError('Outer radius parameter must be greater than 0')
            return False
        self.outer_radius_dyn = QgsProcessingParameters.isDynamic(parameters, self.PrmOuterRadius)
        if self.outer_radius_dyn:
            self.outer_radius_property = parameters[self.PrmOuterRadius]
        self.inner_radius = self.parameterAsDouble(parameters, self.PrmInnerRadius, context)
        self.inner_radius_dyn = QgsProcessingParameters.isDynamic(parameters, self.PrmInnerRadius)
        if self.inner_radius_dyn:
            self.inner_radius_property = parameters[self.PrmInnerRadius]
        segments = self.parameterAsInt(parameters, self.PrmDrawingSegments, context)
        units = self.parameterAsInt(parameters, self.PrmUnitsOfMeasure, context)
        self.export_geom = self.parameterAsBool(parameters, self.PrmExportInputGeometry, context)

        self.measure_factor = conversionToMeters(units)

        self.inner_radius_converted = self.inner_radius * self.measure_factor
        self.outer_radius_converted = self.outer_radius * self.measure_factor

        self.pt_spacing = 360.0 / segments
        source = self.parameterAsSource(parameters, 'INPUT', context)
        src_crs = source.sourceCrs()
        self.total_features = source.featureCount()

        if src_crs != epsg4326:
            self.geom_to_4326 = QgsCoordinateTransform(src_crs, epsg4326, QgsProject.instance())
            self.to_sink_crs = QgsCoordinateTransform(epsg4326, src_crs, QgsProject.instance())
        else:
            self.geom_to_4326 = None
            self.to_sink_crs = None
        self.num_bad = 0
        return True

    def processFeature(self, feature, context, feedback):
        try:
            pts_in = []
            pts_out = []
            pt = feature.geometry().asPoint()
            pt_orig_x = pt.x()
            pt_orig_y = pt.y()
            # make sure the coordinates are in EPSG:4326
            if self.geom_to_4326:
                pt = self.geom_to_4326.transform(pt.x(), pt.y())
            lat = pt.y()
            lon = pt.x()
            if self.inner_radius_dyn:
                inner_rad, e = self.inner_radius_property.valueAsDouble(context.expressionContext(), self.inner_radius)
                if not e:
                    self.num_bad += 1
                    return []
                inner_rad *= self.measure_factor
            else:
                inner_rad = self.inner_radius_converted
            if self.outer_radius_dyn:
                outer_rad, e = self.outer_radius_property.valueAsDouble(context.expressionContext(), self.outer_radius)
                outer_rad *= self.measure_factor
                if not e or outer_rad <= 0:
                    self.num_bad += 1
                    return []
            else:
                outer_rad = self.outer_radius_converted
            angle = 0
            while angle < 360:
                if inner_rad != 0:
                    g = geod.Direct(lat, lon, angle, inner_rad, Geodesic.LATITUDE | Geodesic.LONGITUDE)
                    pts_in.append(QgsPointXY(g['lon2'], g['lat2']))
                g = geod.Direct(lat, lon, angle, outer_rad, Geodesic.LATITUDE | Geodesic.LONGITUDE)
                pts_out.append(QgsPointXY(g['lon2'], g['lat2']))
                angle += self.pt_spacing
            if inner_rad != 0:
                pts_in.append(pts_in[0])
            pts_out.append(pts_out[0])
            crosses_idl = hasIdlCrossing(pts_out)
            if crosses_idl:
                if inner_rad != 0:
                    makeIdlCrossingsPositive(pts_in, True)
                makeIdlCrossingsPositive(pts_out, True)

            # If the Output crs is not 4326 transform the points to the proper crs
            if self.to_sink_crs:
                if inner_rad != 0:
                    for x, pt_out in enumerate(pts_in):
                        pts_in[x] = self.to_sink_crs.transform(pt_out)
                for x, pt_out in enumerate(pts_out):
                    pts_out[x] = self.to_sink_crs.transform(pt_out)

            if self.shape_type == 0:
                if inner_rad == 0:
                    feature.setGeometry(QgsGeometry.fromPolygonXY([pts_out]))
                else:
                    feature.setGeometry(QgsGeometry.fromPolygonXY([pts_out, pts_in]))
            else:
                if inner_rad == 0:
                    feature.setGeometry(QgsGeometry.fromMultiPolylineXY([pts_out]))
                else:
                    feature.setGeometry(QgsGeometry.fromMultiPolylineXY([pts_out, pts_in]))
            if self.export_geom:
                attr = feature.attributes()
                attr.append(pt_orig_x)
                attr.append(pt_orig_y)
                feature.setAttributes(attr)
        except Exception:
            self.num_bad += 1
            return []
        return [feature]

    def postProcessAlgorithm(self, context, feedback):
        if self.num_bad:
            feedback.pushInfo(tr("{} out of {} features had invalid parameters and were ignored.".format(self.num_bad, self.total_features)))
        return {}
