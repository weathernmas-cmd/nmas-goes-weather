# NMAS GOES FEEDER — instalación gratuita

Este alimentador convierte productos científicos NOAA GOES en un JSON pequeño que
Google Apps Script puede consumir sin intentar leer NetCDF dentro de Sheets.

## Qué utiliza

- GOES-19 / GOES-East.
- GOES-18 / GOES-West.
- ABI-L2-ACMF: máscara y probabilidad de nube.
- ABI-L2-RRQPEF: tasa de lluvia satelital.
- Las 38 ciudades y 16 alcaldías que ya existen en WEATHER.gs.

## Instalación en GitHub

1. Crea un repositorio público, por ejemplo: `nmas-goes-weather`.
2. Descomprime `NMAS_GOES_FEEDER_FREE.zip`.
3. Sube TODO su contenido al repositorio. Debe conservarse esta ruta:
   `.github/workflows/update_goes.yml`
4. En GitHub abre:
   `Settings → Actions → General → Workflow permissions`
5. Marca:
   `Read and write permissions`
   y guarda.
6. Abre la pestaña `Actions`.
7. Habilita los workflows si GitHub lo solicita.
8. Abre `Actualizar satélite NOAA GOES`.
9. Pulsa `Run workflow`.
10. Al terminar se creará la rama `data`.

La URL que debes pegar en Google Sheets tendrá esta forma:

`https://raw.githubusercontent.com/TU_USUARIO/TU_REPOSITORIO/data/satellite.json`

## Conexión con WEATHER V18

1. En Apps Script pega la versión original V18 dentro de `WEATHER.gs`.
2. Guarda y ejecuta `SISTEMA_Activar`.
3. Recarga Google Sheets.
4. Abre:
   `🌦️ SISTEMA CLIMA → 🛰️ Configurar satélite NOAA`
5. Pega la URL RAW.
6. Abre:
   `🌦️ SISTEMA CLIMA → 🛰️ Probar satélite`
7. Revisa:
   `🌦️ SISTEMA CLIMA → 🩺 Diagnóstico 24/7`

El diagnóstico ideal mostrará cerca de 54 ubicaciones GOES válidas y una edad menor
a 35 minutos.

## Comportamiento

- GitHub intenta procesar los productos cada 10 minutos.
- El botón `Actualizar Ahora` evita la caché y vuelve a consultar el JSON de inmediato.
- AICM/METAR no interviene en CDMX ni en ninguna de las 16 alcaldías.
- Si el feeder falla, WEATHER continúa con Open-Meteo, SMN y caché.
- RRQPE es una estimación satelital. WEATHER exige persistencia o corroboración para
  reducir falsos positivos.
- Para retirar lluvia se requieren dos lecturas secas consecutivas.
- No se agregan columnas ni se cambian las rutas actuales de Vizrt.

## Importante

El repositorio debe ser público para que Apps Script pueda leer `satellite.json`
sin credenciales. El alimentador no necesita cuenta de AWS ni Google Cloud.
