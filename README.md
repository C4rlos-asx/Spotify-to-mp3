# Spotify a MP3 (CLI)

Convierte URLs de Spotify (track, álbum o playlist) a MP3. Usa Spotify para metadatos y YouTube (yt-dlp) como fuente de audio. Etiqueta los MP3 con ID3 (título, artistas, álbum, número de pista) y carátula.

## Requisitos
- Python 3.9+
- ffmpeg en PATH (necesario para convertir a MP3)
  - Windows: descarga desde https://www.gyan.dev/ffmpeg/builds/ o https://ffmpeg.org/ y agrega `bin/` al PATH.

## Instalación
1. Clona o copia este proyecto.
2. Crea un entorno virtual (opcional pero recomendado):
   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # Windows
   ```
3. Instala dependencias:
   ```bash
   pip install -r requirements.txt
   ```
4. Crea una app en Spotify Developer Dashboard para obtener `SPOTIFY_CLIENT_ID` y `SPOTIFY_CLIENT_SECRET`: https://developer.spotify.com/dashboard
5. Copia `.env.example` a `.env` y rellena tus credenciales:
   ```ini
   SPOTIFY_CLIENT_ID=tu_client_id
   SPOTIFY_CLIENT_SECRET=tu_client_secret
   OUTPUT_DIR=downloads
   ```

## Uso
```bash
python spotify_to_mp3.py "https://open.spotify.com/track/XXXXXXXX"
python spotify_to_mp3.py "https://open.spotify.com/album/YYYYYYYY"
python spotify_to_mp3.py "https://open.spotify.com/playlist/ZZZZZZZZ" --out "mis_descargas"
```

- `--out` (opcional): directorio de salida. Por defecto usa `OUTPUT_DIR` del `.env` o `downloads`.

## Cómo funciona
1. Lee metadatos de Spotify (título, artistas, álbum, carátula).
2. Busca en YouTube usando una query del tipo "Artista - Título official audio".
3. Descarga el mejor audio con `yt-dlp` y lo convierte a MP3 (requiere ffmpeg).
4. Inserta etiquetas ID3 y carátula con `mutagen`.

## Consejos
- Si la carátula no aparece, puede ser una limitación del archivo o del reproductor. Se vuelve a intentar en próximas descargas.
- Cambia la query en `yt_search_query()` en `spotify_to_mp3.py` si quieres otro patrón de búsqueda.
- Si `ffmpeg` no está en PATH, la conversión fallará. Verifica ejecutando `ffmpeg -version` en la terminal.

## Problemas comunes
- "Missing SPOTIFY_CLIENT_ID/SECRET": Asegúrate de crear `.env` a partir de `.env.example`.
- Fallo descargando con yt-dlp: Es normal que falle algún intento. El script reintenta automáticamente.
- Permisos/Firewall: En Windows, ejecuta la terminal como Administrador si hay errores de red o permisos.

## Licencia
Uso personal/educativo. Verifica las leyes locales de derechos de autor antes de descargar contenido.
