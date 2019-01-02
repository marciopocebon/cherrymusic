from django.conf import settings
from django.db import models
from pathlib import Path
import logging
from django.utils import timezone

from ext.tinytag import TinyTag, TinyTagException
from utils.natural_language import normalize_name

logger = logging.getLogger(__name__)


class Directory(models.Model):
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='subdirectories')
    path = models.CharField(max_length=255)

    def __str__(self):
        return self.path

    def absolute_path(self):
        if not hasattr(self, '_cached_absolute_path'):
            if self.parent is None: # only the basedir has no parent
                self._cached_absolute_path = Path(self.path)
            else:
                self._cached_absolute_path = self.parent.absolute_path() / self.path
        return self._cached_absolute_path

    def relative_path(self):
        if self.parent is None:
            return Path()
        else:
            return self.parent.relative_path() / self.path

    def get_sub_path_directories(self, path_elements):
        current, *rest = path_elements
        # check if the directory exists in database on the fly
        try:
            dir = Directory.objects.get(parent=self, path=current)
        except Directory.DoesNotExist:
            # try to index the directory if it does not exist yet
            dir = Directory(parent=self, path=current)
            if dir.exists():
                dir.save()
            else:
                raise FileNotFoundError('The directory %s does not exist!' % dir.absolute_path())
        if rest:
            return [dir] + dir.get_sub_path_directories(rest)
        else:
            return [dir]

    def listdir(self):
        return (
            File.objects.filter(directory=self).order_by('filename').all(),
            Directory.objects.filter(parent=self).order_by('path').all(),
        )

    def exists(self):
        return self.absolute_path().exists()

    def reindex(self, recursively=True):
        deleted_files = 0
        deleted_directories = 0
        indexed_files = 0
        indexed_directories = 0
        # remove all stale files
        for f in self.file_set.all():
            print(f)
            if not f.exists():
                f.delete()
                deleted_files += 1
        # remove all stale directories:
        for d in Directory.objects.filter(parent=self):
            if not d.exists():
                d.delete()
                deleted_directories += 1
        # add all files and directories
        for sub_path in Path(self.absolute_path()).iterdir():
            if sub_path.is_file():
                # index all indexable files
                f = File(filename=sub_path.name, directory=self)
                if f.indexable():
                    try:
                        # check if the file was already indexed:
                        f = File.objects.get(filename=sub_path.name, directory=self)
                    except File.DoesNotExist:
                        f.save()
                        indexed_files += 1
            elif sub_path.is_dir():
                if sub_path.name == '.':
                    continue
                sub_dir, created = Directory.objects.get_or_create(parent=self, path=sub_path.name)
                if created:
                    indexed_directories += 1

                if recursively:
                    # index everything recursively, keep track of total files indexed
                    del_files, del_directories, idx_files, idx_directories = sub_dir.reindex()
                    deleted_files += del_files
                    deleted_directories += del_directories
                    indexed_files += idx_files
                    indexed_directories += idx_directories
            else:
                logger.info('Unknown filetype %s', sub_path)
        return (
            deleted_files,
            deleted_directories,
            indexed_files,
            indexed_directories,
        )


class Artist(models.Model):
    name = models.CharField(max_length=255)
    norm_name = models.CharField(max_length=255)

    def __str__(self):
        return self.name

    def save(self, force_insert=False, force_update=False, using=None,
             update_fields=None):
        self.norm_name = normalize_name(self.name)
        super().save(force_insert=False, force_update=False, using=None,
             update_fields=None)

    @classmethod
    def get_for_name(cls, name):
        if not name:
            return
        norm_name = normalize_name(name)
        if not norm_name:
            return
        artist, created = cls.objects.get_or_create(
            norm_name=norm_name,
            defaults=dict(
                name=name,
            )
        )
        return artist


class Album(models.Model):
    name = models.CharField(max_length=255)
    albumartist = models.ForeignKey(
        Artist,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name='albums'
    )

    @classmethod
    def get_for_name(cls, album, albumartist):
        return cls.objects.get_or_create(name=album, albumartist=albumartist)[0]


class Genre(models.Model):
    name = models.CharField(max_length=255)

    def save(self, *args, **kwargs):
        self.name = normalize_name(self.name)
        super().save(*args, **kwargs)

    @classmethod
    def get_for_name(cls, genre):
        return Genre.objects.get_or_create(name=normalize_name(genre))[0]


class MetaData(models.Model):
    track = models.IntegerField(null=True, blank=True)
    track_total = models.IntegerField(null=True, blank=True)
    title = models.CharField(max_length=255, null=True, blank=True)
    artist = models.ForeignKey(Artist, null=True, blank=True, on_delete=models.CASCADE, related_name='tracks')
    album = models.ForeignKey(Album, null=True, blank=True, on_delete=models.PROTECT, related_name='tracks')
    year = models.IntegerField(null=True, blank=True)
    genre = models.ForeignKey(Genre, null=True, blank=True, on_delete=models.PROTECT)
    duration = models.FloatField(null=True, blank=True)

    def __str__(self):
        return f'{self.track} {self.artist} - {self.title}'

    @classmethod
    def create_from_path(cls, path):
        try:
            tag = TinyTag.get(path)
        except TinyTagException:
            return
        artist = Artist.get_for_name(tag.artist)
        albumartist = Artist.get_for_name(tag.albumartist)
        album = Album.get_for_name(tag.album, albumartist or artist) if tag.album else None
        genre = Genre.get_for_name(tag.genre) if tag.genre else None
        meta_data = MetaData.objects.create(
            track=int(tag.track) if tag.track else None,
            track_total=int(tag.track_total) if tag.track_total else None,
            title=tag.title,
            artist=artist,
            album=album,
            year=int(tag.year) if tag.year else None,
            genre=genre,
            duration=tag.duration,
        )
        return meta_data


class File(models.Model):
    filename = models.CharField(max_length=255)
    directory = models.ForeignKey(Directory, on_delete=models.PROTECT, related_name='files')

    meta_indexed_at = models.DateField(null=True, blank=True)
    meta_data = models.OneToOneField(MetaData, null=True, blank=True, on_delete=models.SET_NULL)

    def update_metadata(self):
        meta_data = MetaData.create_from_path(str(self.absolute_path()))
        self.meta_data = meta_data
        self.meta_indexed_at = timezone.now()
        self.save()

    @classmethod
    def index_unindexed_metadata(cls):
        for f in File.objects.filter(meta_indexed_at__isnull=True):
            f.update_metadata()

    def __str__(self):
        return self.filename

    def indexable(self):
        supported_file_types = tuple(settings.SUPPORTED_FILETYPES)
        return self.filename.lower().endswith(supported_file_types)

    def absolute_path(self):
        return self.directory.absolute_path() / self.filename

    def relative_path(self):
        return self.directory.relative_path() / self.filename

    def exists(self):
        return self.absolute_path().exists()