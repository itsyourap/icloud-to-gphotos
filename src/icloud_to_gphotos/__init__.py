"""One-way migration of iCloud Photos into Google Photos.

Downloads originals from iCloud with pyicloud, uploads them to Google Photos with
the gotohp CLI, and only then deletes the originals from iCloud — never before
Google Photos has confirmed the upload.
"""

__version__ = "0.1.0"
