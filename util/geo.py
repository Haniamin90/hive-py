import argparse
import geopy.distance
import json
import math
import shapefile

from geographiclib.geodesic import Geodesic
from tqdm import tqdm
from pyproj import Transformer

DEFAULT_WIDTH = 25
MERCATOR_TO_WGS = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
WGS_TO_MERCATOR = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)

def abs_angular_delta(a, b):
  delta = abs(a - b)
  return delta if delta <= 180 else 360 - delta

def angle_between_segments(p0, p1, p2):
  p0lon, p0lat = p0
  p1lon, p1lat = p1
  p2lon, p2lat = p2

  azi_a = Geodesic.WGS84.Inverse(p0lat, p0lon, p1lat, p1lon).get('azi2')
  azi_b = Geodesic.WGS84.Inverse(p1lat, p1lon, p2lat, p2lon).get('azi2')
  return abs_angular_delta(azi_a, azi_b)

def get_coords(feature):
  geo = feature.get('geometry', feature)
  return geo.get('coordinates')

def filter_small_segments(linestring, min_length):
  coords = get_coords(linestring)

  if len(coords) < 2:
    return coords

  new_coords = [coords[0]]

  prev = coords[0]

  for i in range(1, len(coords)):
    cur = coords[i]
    d = geopy.distance.distance(
      reversed(cur),
      reversed(prev),
    ).meters

    if d < min_length:
      continue

    new_coords.append(cur)
    prev = cur

  return new_coords

def point_to_square(coord, width):
  half_width = float(width) / 2.0
  new_coords = []

  cx, cy = WGS_TO_MERCATOR.transform(*coord)

  new_coords.append(MERCATOR_TO_WGS.transform(cx - half_width, cy - half_width))
  new_coords.append(MERCATOR_TO_WGS.transform(cx + half_width, cy - half_width))
  new_coords.append(MERCATOR_TO_WGS.transform(cx + half_width, cy + half_width))
  new_coords.append(MERCATOR_TO_WGS.transform(cx - half_width, cy + half_width))
  new_coords.append(new_coords[0])

  return {
    "type": "Feature",
    "properties": {},
    "geometry": {
      "type": "Polygon",
      "coordinates": [new_coords],
    },
  }

def explode_sharp_angles(coords, threshold = 45):
  if len(coords) < 3:
    return [coords]

  lines = []
  cur_line = [coords[0], coords[1]]

  for i in range(2, len(coords)):
    theta = angle_between_segments(coords[i - 2], coords[i - 1], coords[i])
    if theta <= threshold:
      cur_line.append(coords[i])
    else:
      lines.append(cur_line)
      # also add the one-point segment because we don't
      # support a miter/join solution for continuous solution
      lines.append([coords[i - 1]])
      cur_line = [coords[i - 1], coords[i]]

  lines.append(cur_line)

  return lines

def geojson_point_to_poly(
  point,
  width = DEFAULT_WIDTH,
):
  coord = get_coords(point)
  return point_to_square(coord, width)

def geojson_linestring_to_poly(
  linestring,
  width = DEFAULT_WIDTH,
):
  filtered_coords = filter_small_segments(linestring, width)
  half_width = float(width) / 2.0

  n = 2 * len(filtered_coords)

  new_coords = [None] * (n + 1)

  # if we're left with 1 point, convert to a box
  if len(filtered_coords) < 2:
    return point_to_square(filtered_coords[0], width)

  lines = explode_sharp_angles(filtered_coords)

  if len(lines) > 1:
    linestrings = [{
      "type": "LineString",
      "coordinates": line,
    } for line in lines]

    polys = [geojson_linestring_to_poly(line) for line in linestrings]
    return {
      "type": "Feature",
      "properties": {},
      "geometry": {
        "type": "MultiPolygon",
        "coordinates": [
          poly.get('geometry').get('coordinates') for poly in polys
        ],
      },
    }
  else:
    p0x, p0y = WGS_TO_MERCATOR.transform(*filtered_coords[0])
    p1x, p1y = WGS_TO_MERCATOR.transform(*filtered_coords[1])

    dx, dy = (p1x - p0x, p1y - p0y)
    mag = math.sqrt(dx ** 2 + dy ** 2)
    dx /= mag
    dy /= mag
    nx, ny = (-dy * half_width, dx * half_width)

    new_coords[0] = MERCATOR_TO_WGS.transform(p0x + nx, p0y + ny)
    new_coords[n - 1] = MERCATOR_TO_WGS.transform(p0x - nx, p0y - ny)
    new_coords[n] = new_coords[0]

    for i in range(1, len(filtered_coords)):
      p0x, p0y = WGS_TO_MERCATOR.transform(*filtered_coords[i - 1])
      p1x, p1y = WGS_TO_MERCATOR.transform(*filtered_coords[i])

      dx, dy = (p1x - p0x, p1y - p0y)
      mag = math.sqrt(dx ** 2 + dy ** 2)
      dx /= mag
      dy /= mag
      nx, ny = (-dy * half_width, dx * half_width)

      new_coords[i] = MERCATOR_TO_WGS.transform(p1x + nx, p1y + ny)
      new_coords[n - i - 1] = MERCATOR_TO_WGS.transform(p1x - nx, p1y - ny)

    return {
      "type": "Feature",
      "properties": {},
      "geometry": {
        "type": "Polygon",
        "coordinates": [new_coords],
      },
    }

def convert_to_geojson_poly(feature, width = DEFAULT_WIDTH):
  geom = feature['geometry']
  t = geom['type']

  if t == 'LineString':
    return geojson_linestring_to_poly(geom, width)
  elif t == 'Point':
    return geojson_point_to_poly(geom, width)
  elif t == 'Polygon' or t == 'MultiPolygon':
    return geom
  else:
    raise Exception(f'Unsupported type: {t}')

def transform_shapefile_to_geojson_polygons(file_path, width = DEFAULT_WIDTH, verbose = False):
  geojson = {}

  if verbose:
    print(f'reading {file_path} as geojson...')
  with shapefile.Reader(file_path) as shp:
    geojson = shp.__geo_interface__

  features = geojson.get('features')
  if verbose:
    print(f'converting {len(features)} features to polygons...')

  if verbose:
    polygons = []
    for f in tqdm(features):
      polygons.append(convert_to_geojson_poly(f, width))
  else:
    polygons = [convert_to_geojson_poly(f, width) for f in features]

  return polygons

if __name__ == '__main__':
  parser = argparse.ArgumentParser()
  parser.add_argument('-s', '--shapefile', type=str, required=True)
  parser.add_argument('-o', '--output_json', type=str, required=True)
  parser.add_argument('-w', '--width', type=int, default=DEFAULT_WIDTH)
  parser.add_argument('-q', '--quiet', action='store_true')
  args = parser.parse_args()

  features = transform_shapefile_to_geojson_polygons(
    args.shapefile,
    args.width,
    not args.quiet,
  )

  with open(args.output_json, 'w') as f:
    json.dump({
      'type': 'FeatureCollection',
      'features': features,
      }, f)
