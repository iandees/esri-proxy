from flask_cache import Cache
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import FlaskForm
from wtforms import StringField
from wtforms.validators import DataRequired
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for
)
from geoalchemy2 import Geometry
from geoalchemy2.shape import to_shape
from shapely.geometry import mapping
from sqlalchemy.orm import load_only
from PIL import Image
from cStringIO import StringIO
import json
import math
import mercantile
import requests
import uuid
import urlparse
import urllib


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
    url_template = db.Column(db.Text)
    bbox = db.Column(Geometry(geometry_type='POLYGON', srid=4326), index=True)
    min_zoom = db.Column(db.Integer)
    max_zoom = db.Column(db.Integer)


class NewEsriSourceForm(FlaskForm):
    name = StringField('name', validators=[DataRequired()])
    url = StringField('url', validators=[DataRequired()])


@app.route('/v1/tiles/<layer>/<int:zoom>/<int:x>/<int:y>.<fmt>')
@cache.cached()
def get_tile(layer, zoom, x, y, fmt):
    (min_lon, min_lat, max_lon, max_lat) = mercantile.bounds(x, y, zoom)

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

    composite = Image.new('RGBA', (256, 256))
    for source in sources:
        url = source.url_template.format(
            xmin=min_lon,
            ymin=min_lat,
            xmax=max_lon,
            ymax=max_lat,
            width=256,
            height=256,
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
        composite.save(out_buff, 'jpeg', quality=35)
        mimetype = 'image/jpeg'
    elif fmt in ('png',):
        composite.save(out_buff, 'png', optimize=True)
        mimetype = 'image/png'
    out_buff.seek(0)

    return send_file(out_buff, mimetype=mimetype)


def scale_to_zoom(scale):
    if not scale:
        return None

    return int(round(-1.443 * math.log(scale) + 29.14))


def build_esri_source(name, url):
    url_parts = urlparse.urlparse(url)
    query_url_parts = urlparse.urlparse(url_parts.query)
    proxy_parts = None
    if query_url_parts.scheme:
        service_parts = query_url_parts
        proxy_parts = url_parts
    else:
        service_parts = url_parts

    service_type = service_parts.path.rstrip('/').rsplit('/', 1)[-1]
    if service_type not in ('MapServer', 'ImageServer'):
        raise ValueError("The layer doesn't seem to be a MapServer or ImageServer")

    if proxy_parts:
        proxied_metadata = urlparse.urlunparse(service_parts._replace(query='f=json'))
        metadata_parts = url_parts._replace(query=proxied_metadata)
    else:
        metadata_parts = url_parts._replace(query='f=json')
    metadata_url = urlparse.urlunparse(metadata_parts)
    resp = requests.get(metadata_url)

    if resp.status_code != 200:
        raise ValueError("Error retrieving layer metadata from " + resp.request.url)

    metadata = resp.json()

    base_url = urlparse.urlunparse(service_parts)
    capabilities = metadata.get('capabilities')
    if service_type == 'ImageServer':
        if 'Image' not in capabilities:
            raise ValueError("The layer doesn't seem to support image export")

        url_template = base_url + '/exportImage'
    elif service_type == 'MapServer':
        if 'Map' not in capabilities:
            raise ValueError("The layer doesn't seem to support map export")

        url_template = base_url + '/export'

    url_template = url_template + (
        '?bbox={xmin},{ymin},{xmax},{ymax}'
        '&bboxSR=4326&size={width},{height}'
        '&imageSR=102113&transparent=true'
        '&format=png&f=image'
    )

    extent = metadata.get('fullExtent')
    extent_sr = extent.get('spatialReference')
    from_sr = extent_sr.get('latestWkid') or extent_sr.get('wkid') or extent_sr.get('wkt')
    proj_params = {
        'f': 'json',
        'inSR': from_sr,
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
        raise ValueError("Problem projecting bounding box: {}".format(resp.json().get('error')))
    projected = resp.json()['geometries'][0]
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


@app.route('/sources', methods=['GET', 'POST'])
def show_sources():
    esri_form = NewEsriSourceForm()

    if esri_form.validate_on_submit():
        source = build_esri_source(esri_form.name.data, esri_form.url.data)
        db.session.add(source)
        db.session.commit()

    sources = Source.query.filter()

    return render_template(
        'index.html',
        sources=sources,
        esri_form=esri_form,
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
    shape = to_shape(source.bbox)
    geojson = mapping(shape)

    return jsonify({
        'type': "Feature",
        'id': source.slug,
        'properties': {
            'name': source.name,
            'slug': source.slug,
            'url_template': source.url_template,
            'min_zoom': source.min_zoom,
            'max_zoom': source.max_zoom,
        },
        'geometry': geojson
    })


if __name__ == '__main__':
    app.run()
