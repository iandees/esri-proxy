from flask_caching import Cache
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import FlaskForm
from wtforms import SelectField, StringField
from wtforms.validators import DataRequired
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for
)
from geoalchemy2 import Geometry
import geoalchemy2.functions as geofunc
from sqlalchemy.orm import load_only
from PIL import Image
from cStringIO import StringIO
import json
import math
import mercantile
import requests
import uuid
import urlparse


app = Flask(__name__)
app.config.from_object('config')
cache = Cache(app)
db = SQLAlchemy(app)
migrate = Migrate(app, db)


class Source(db.Model):
    __tablename__ = "sources"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), index=True, unique=True)
    name = db.Column(db.String(256))
    vintage = db.Column(db.Date)
    resolution = db.Column(db.Numeric(5, 2))
    resolution_unit = db.Column(db.String(12))
    resolution_meters = db.Column(db.Numeric(5, 2))
    url_template = db.Column(db.Text)
    bbox = db.Column(Geometry(geometry_type='POLYGON', srid=4326), index=True)
    min_zoom = db.Column(db.Integer)
    max_zoom = db.Column(db.Integer)

    @property
    def resolution_str(self):
        if self.resolution:
            return u"{}{}".format(
                self.resolution,
                self.resolution_unit,
            )
        else:
            return u"unknown"


class NewEsriSourceForm(FlaskForm):
    name = StringField('Name', validators=[DataRequired()])
    url = StringField('url', validators=[DataRequired()])
    vintage = StringField('Vintage')
    resolution = StringField('Resolution')
    resolution_unit = SelectField(
        choices=[
            ('cm', 'cm/pixel'),
            ('m', 'm/pixel'),
            ('in', 'in/pixel'),
            ('ft', 'feet/pixel'),
        ]
    )


@app.route('/v1/tiles/<layer>/<int:zoom>/<int:x>/<int:y>.<fmt>')
@app.route('/v1/tiles/<layer>/<int:zoom>/<int:x>/<int:y>@<int:scale>x.<fmt>')
@cache.cached(300)
def get_tile(layer, zoom, x, y, fmt, scale=1):
    (min_lon, min_lat, max_lon, max_lat) = mercantile.bounds(x, y, zoom)
    (min_x, min_y) = mercantile.xy(min_lon, min_lat)
    (max_x, max_y) = mercantile.xy(max_lon, max_lat)

    sources = Source.query.options(load_only(Source.url_template)).filter(
        Source.min_zoom <= zoom,
        Source.max_zoom > zoom,
        Source.bbox.ST_Intersects(
            'SRID=4326;POLYGON(({min_lon} {min_lat}, {min_lon} {max_lat}, '
            '{max_lon} {max_lat}, {max_lon} {min_lat}, '
            '{min_lon} {min_lat}))'.format(
                min_lon=min_lon,
                min_lat=min_lat,
                max_lon=max_lon,
                max_lat=max_lat,
            )
        )
    )
    if layer != 'global':
        sources = sources.filter(
            Source.slug == layer
        )
    sources = sources.all()

    if not sources:
        abort(404, 'No sources for that tile')

    width = 256 * scale
    height = 256 * scale
    composite_shape = (width, height)
    composite = Image.new('RGBA', composite_shape)
    for source in sources:
        url = source.url_template.format(
            min_lon=min_lon,
            min_lat=min_lat,
            max_lon=max_lon,
            max_lat=max_lat,
            min_x=min_x,
            min_y=min_y,
            max_x=max_x,
            max_y=max_y,
            width=width,
            height=height,
        )
        resp = requests.get(url)
        resp.raise_for_status()
        content = StringIO(resp.content)
        image = Image.open(content)
        image = image.convert('RGBA')
        # See http://stackoverflow.com/a/5324782
        composite = Image.alpha_composite(composite, image)

    out_buff = StringIO()
    if fmt in ('jpeg', 'jpg'):
        quality = int(app.config.get('JPEG_QUALITY'))
        composite.save(out_buff, 'jpeg', quality=quality)
        content_type = 'image/jpeg'
    elif fmt in ('png',):
        composite.save(out_buff, 'png', optimize=True)
        content_type = 'image/png'
    else:
        abort(404, 'Unknown format specified')
    out_buff.seek(0)

    resp = make_response(out_buff.read())
    resp.headers['Content-Type'] = content_type
    return resp


def scale_to_zoom(scale):
    if not scale:
        return None

    return int(round(-1.443 * math.log(scale) + 29.14))


def parse_base_url(user_url):
    url_parts = urlparse.urlparse(user_url)
    query_url_parts = urlparse.urlparse(url_parts.query)
    proxy_parts = None
    token = None
    if query_url_parts.scheme:
        service_parts = query_url_parts
        proxy_parts = url_parts
    else:
        service_parts = url_parts

        qs = urlparse.parse_qs(service_parts.query)
        if qs and qs.get('token'):
            token = qs.get('token')[0]

    service_type = service_parts.path.rstrip('/').rsplit('/', 1)[-1]
    if service_type not in ('MapServer', 'ImageServer'):
        raise ValueError("The layer doesn't seem to be a MapServer or ImageServer")

    if proxy_parts:
        proxied_metadata = urlparse.urlunparse(service_parts)
        base_url = urlparse.urlunparse(url_parts._replace(query=proxied_metadata))
    else:
        base_url = urlparse.urlunparse(url_parts._replace(query=None))

    return service_type, base_url, token


def build_metadata_url(user_url):
    service_type, base_url, token = parse_base_url(user_url)

    query_part = '?f=json'

    if token:
        query_part += '&token=' + token

    return base_url + query_part


def build_url_template(user_url):
    service_type, base_url, token = parse_base_url(user_url)

    if service_type == 'ImageServer':
        url_template = base_url + '/exportImage'
    elif service_type == 'MapServer':
        url_template = base_url + '/export'

    query_part = (
        '?bbox={min_x},{min_y},{max_x},{max_y}'
        '&bboxSR=102113&size={width},{height}'
        '&imageSR=102113&transparent=true'
        '&format=png&f=image'
    )

    if token:
        query_part += '&token=' + token

    return url_template + query_part


def project(extent):
    extent_sr = extent.pop('spatialReference')
    proj_params = {
        'f': 'json',
        'inSR': json.dumps(extent_sr),
        'outSR': '4326',
        'geometries': json.dumps({
            'geometryType': 'esriGeometryEnvelope',
            'geometries': [extent],
        })
    }
    resp = requests.get('http://sampleserver1.arcgisonline.com/ArcGIS/rest/services/Geometry/GeometryServer/project', params=proj_params)
    if resp.status_code != 200:
        raise ValueError("Couldn't project layer bounding box")

    if resp.json().get('error'):
        raise ValueError("Problem projecting bounding box: {}; {}".format(
            resp.json().get('error'),
            resp.request.url))

    return resp.json()['geometries'][0]


def build_esri_source(name, url):
    metadata_url = build_metadata_url(url)
    url_template = build_url_template(url)

    resp = requests.get(metadata_url)

    if resp.status_code != 200:
        raise ValueError("Error retrieving layer metadata from " + resp.request.url)

    metadata = resp.json()
    extent = metadata.get('fullExtent') or metadata.get('extent')
    projected = project(extent)

    bbox = ('SRID=4326;POLYGON(({xmin} {ymin}, {xmin} {ymax}, '
            '{xmax} {ymax}, {xmax} {ymin}, {xmin} {ymin}))'.format(
                **projected
            ))

    slug = str(uuid.uuid4())[:8]
    source = Source(
        slug=slug,
        name=name,
        url_template=url_template,
        bbox=bbox,
        min_zoom=scale_to_zoom(metadata.get('minScale')) or 0,
        max_zoom=scale_to_zoom(metadata.get('maxScale')) or 22,
    )

    return source


@app.route('/sources')
def show_sources():
    sources = Source.query.filter()

    return render_template(
        'index.html',
        sources=sources,
    )


@app.route('/sources/global')
def show_global():
    return render_template(
        'global.html',
    )


@app.route('/sources/<slug>')
def show_source(slug):
    source = Source.query.filter_by(slug=slug).first_or_404()

    esri_form = NewEsriSourceForm()

    if esri_form.validate_on_submit():
        source = build_esri_source(esri_form.name.data, esri_form.url.data)
        db.session.add(source)
        db.session.commit()

    return render_template(
        'show_source.html',
        source=source,
        esri_form=esri_form,
    )


def normalize_resolution(res, res_unit):
    if res_unit == 'm':
        return res
    elif res_unit == 'cm':
        return res / 100.0
    elif res_unit == 'in':
        return res / 0.0254
    elif res_unit == 'ft':
        return res / 0.3048
    else:
        raise TypeError("Unknown resolution unit")


@app.route('/sources/add', methods=['GET', 'POST'])
def add_source():
    esri_form = NewEsriSourceForm()

    if esri_form.validate_on_submit():
        source = build_esri_source(esri_form.name.data, esri_form.url.data)
        source.vintage = esri_form.vintage.data

        if esri_form.resolution.data:
            source.resolution = esri_form.resolution.data
            source.resolution_unit = esri_form.resolution_unit.data
            source.resolution_meters = normalize_resolution(
                float(source.resolution),
                source.resolution_unit
            )

        db.session.add(source)
        db.session.commit()

        flash('Your source was added')
        return redirect(url_for('show_source', slug=source.slug))

    return render_template(
        'new_source.html',
        esri_form=esri_form,
    )


@app.route('/sources/<slug>/delete')
def delete_source(slug):
    source = Source.query.filter_by(slug=slug).first_or_404()

    if request.args.get('for_real') == 'true':
        db.session.delete(source)
        db.session.commit()
        flash("Deleted source {}".format(source.name))
        return redirect(url_for('show_sources'))

    return render_template(
        'delete_source.html',
        source=source,
    )


@app.route('/sources/<slug>.geojson')
def show_source_geojson(slug):
    source = Source.query.filter_by(slug=slug).first_or_404()
    geom_json = json.loads(db.session.scalar(geofunc.ST_AsGeoJSON(source.bbox, 6)))

    return jsonify({
        'type': "Feature",
        'id': source.slug,
        'properties': {
            'name': source.name,
            'vintage': source.vintage,
            'resolution': '{}{}'.format(
                source.resolution,
                source.resolution_unit,
            ),
            'slug': source.slug,
            'url_template': source.url_template,
            'min_zoom': source.min_zoom,
            'max_zoom': source.max_zoom,
        },
        'geometry': geom_json,
    })


if __name__ == '__main__':
    app.run()
