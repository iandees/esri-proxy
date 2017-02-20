import os

SQLALCHEMY_TRACK_MODIFICATIONS = False
SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'postgresql://localhost/tileproxy')
SECRET_KEY = 'you-will-never-guess'
CACHE_TYPE = os.environ.get('CACHE_TYPE', 'simple')
CACHE_REDIS_URL = os.environ.get('REDIS_URL')
JPEG_QUALITY = os.environ.get('JPEG_QUALITY', 45)
