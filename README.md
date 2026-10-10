# Backend de fotos

Backend desplegado en Railway: https://fotos-api-production.up.railway.app. Angular envía originales a FastAPI, Drive los conserva privados y Firestore almacena autor, mensaje y referencias. Verificados en el servicio real: subida, vista previa, reintento sin duplicados, descarga íntegra y rechazo de accesos anónimos al álbum.

## Desarrollo

Desde backend: `python -m venv .venv`, instalar `requirements.txt` y ejecutar `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --env-file .env --no-access-log`.
Configurar ALLOWED_ORIGINS en el entorno; .env.example es una referencia. El comando anterior carga .env explícitamente.
GET /health informa uploads_enabled. PHOTOS_ENABLED debe ser true y ambas conexiones deben funcionar; en caso contrario las operaciones devuelven 503.

## Estado de la primera etapa

Angular expone /fotos y usa la URL HTTPS configurada en frontend/public/photos-config.json, también durante el desarrollo local. Si esa configuración está vacía, solo el modo de desarrollo local permite usar 127.0.0.1:8000; en producción mantiene la recepción desactivada. Solo ?instance=demo o el dominio de demo usan IndexedDB, aislado del álbum real. /admin → Fotos verifica en el servidor la misma clave del admin antes de listar o descargar. No se cambiaron reglas remotas: las nuevas colecciones no tienen acceso permitido desde clientes.

## Pendiente para recepción real

La primera versión usa sesiones firmadas de dos horas para el admin y autorizaciones por tanda de 24 horas. La clave se comprueba contra admin/config.masterKeyHash en el servidor; desbloquear el frontend por sí solo no permite descargar archivos. Las sesiones quedan en memoria del navegador. OAuth usa drive.file, con el token de renovación cifrado y separado del JSON de la cuenta de servicio Firebase. La cuenta de servicio escribe en Firestore; la cuenta personal autorizada es propietaria de los archivos de Drive.

Modelo: wedding_photo_batches/{id} guarda nombre, mensaje, fecha, caducidad y número de fotos; wedding_photos/{id} enlaza batchId, driveFileId, thumbnailDriveId, SHA-256, tamaño, formato y estado. wedding_photo_system/usage contabiliza reservas de capacidad. No abrir estas colecciones con reglas de cliente. Los invitados reciben una autorización limitada a su tanda; nunca tokens OAuth de Google.

Se validan los bytes decodificados de JPG, PNG, WebP y HEIC/HEIF: hasta 20 MB, 40 megapíxeles y 20 fotos por tanda. Las vistas previas son JPEG sin EXIF/GPS; los originales conservan sus bytes. Los IDs de Drive se reservan antes de subir, para que los reintentos no dupliquen archivos. Las transferencias backend→Drive usan sesiones reanudables; navegador→backend reintenta el archivo completo. No se recuperan selecciones al cerrar la pestaña.

Hay dos subidas simultáneas, límites por IP en memoria y reservas transaccionales en Firestore: por defecto 5 GiB y 2000 fotos. Los intentos incompletos conservan su reserva para poder reintentarse; su conciliación manual queda pendiente. La galería pagina 30 fotos y busca por nombre, mensaje o archivo en el servidor; la búsqueda recorre metadatos y consume lecturas de Firestore. Mantener una sola instancia/worker mientras se utilice el limitador en memoria.

Antes del evento: probar iPhone y Android reales, la conexión móvil del lugar y la concurrencia esperada. Las pruebas automatizadas cubren HEIC, reintentos, límites y acceso privado. El propietario activó Hobby en Railway. El Dockerfile solo copia app/; los scripts de autorización local no se publican.

## Desplegar en Railway

El backend tiene su propio repositorio: https://github.com/JuanZozaya06/wedding-backend. Esta carpeta backend/ es un checkout independiente, ignorado por el repositorio de la invitación. Railway utiliza la rama main, la raíz /, el Dockerfile y /railway.toml. El contenedor escucha en 0.0.0.0 y en el PORT asignado por Railway. /ready devuelve 200 solo después de validar las conexiones de Drive y Firestore; una configuración incompleta mantiene el despliegue fuera de servicio.

Proyecto actual: boda-fotos (6c9f335e-0eb2-4824-a1e2-c7b72bf7f0d2). Servicio: fotos-api (aa6cab6f-60f1-44e2-90bd-a9304b1fd1ec), entorno production, PORT=8000. Primer despliegue verificado por Railway CLI; las actualizaciones se conectan al repositorio dedicado. Dashboard: https://railway.com/project/6c9f335e-0eb2-4824-a1e2-c7b72bf7f0d2.

GitHub Actions ejecuta Backend CI (Python 3.12 y pruebas sin credenciales reales) en push a main y en pull requests. La fuente del servicio apunta al repositorio dedicado; la activación del despliegue automático y Wait for CI está pendiente de conceder acceso a wedding-backend a la app Railway en GitHub. Hasta completar ese permiso, el backend mantiene el despliegue manual ya verificado.

Pipeline previsto tras autorizar: push a main → Backend CI → Railway con Wait for CI → construcción Docker → /ready. Un fallo de las pruebas impedirá publicar ese commit. Los pull requests ejecutan las pruebas sin desplegar. Las credenciales permanecen en variables privadas del servicio y no se necesitan secretos de Google en GitHub Actions.

Para actualizar desde esta carpeta: revisar git diff, ejecutar las pruebas, hacer commit y git push origin main. El frontend se mantiene en wedding-invitation y conserva su pipeline de GitHub Pages.

Configurar como variables privadas del servicio:

- GOOGLE_CLOUD_PROJECT: proyecto Firebase existente.
- GOOGLE_DRIVE_FOLDER_ID: carpeta ya seleccionada con Picker.
- GOOGLE_DRIVE_THUMBNAILS_FOLDER_ID: ID de .secrets/drive-thumbnails.json. Se reutiliza la carpeta existente en cada arranque.
- DRIVE_CREDENTIALS_ENCRYPTED: contenido de .secrets/drive-credentials.enc, conservando el cifrado.
- DRIVE_TOKEN_ENCRYPTION_KEY: clave local que permite descifrar esa conexión.
- FIREBASE_SERVICE_ACCOUNT_JSON: JSON completo de la cuenta de servicio dedicada a Firestore.
- PHOTO_SIGNING_KEY: clave persistente para las sesiones firmadas; conservarla entre despliegues.
- ALLOWED_ORIGINS: https://labodadelsiglo.app y los orígenes locales necesarios para pruebas.
- PHOTOS_ENABLED: true tras preparar ambas conexiones.
- PHOTO_MAX_TOTAL_BYTES y PHOTO_MAX_COUNT: límites del álbum, por defecto 5 GiB y 2000 fotos.

No subir .env, .secrets ni los JSON descargados en la imagen o al repositorio. No se necesita un volumen: las fotos persisten en Drive, el catálogo en Firestore y las credenciales en variables privadas de Railway. El acceso temporal de Google se renueva en memoria a partir del permiso persistente cifrado. La configuración local mediante archivos sigue funcionando.

Generar el dominio HTTPS del servicio y comprobar /ready, una subida, un reintento y una descarga privada. Después establecer esa URL en frontend/public/photos-config.json y publicar el frontend. Mantener una sola instancia y un worker. Railway Free está sujeto a su crédito y límites; revisar el plan de la cuenta y el consumo antes del evento.

## Conectar la cuenta de Google

El proyecto existente es wedding-invitation-df2db. El ID de carpeta elegido se configura en GOOGLE_DRIVE_FOLDER_ID dentro de .env (ignorado por Git). Esto identifica el destino, pero no concede autorización ni activa las subidas.

1. En Google Cloud, seleccionar el proyecto existente y habilitar Google Drive API y Google Picker API.
2. Configurar Google Auth Platform para uso externo en pruebas con la cuenta propietaria como usuario de prueba.
3. Crear un cliente OAuth de aplicación web cuando estén implementados y fijados los endpoints de callback. No inventar una URL de Railway antes del despliegue.
4. Autorizar drive.file y seleccionar la carpeta existente con Google Picker; copiar un ID no sustituye la selección autorizada.
5. Guardar el secreto OAuth y el token de renovación solo en servidor, y validar administradores antes de exponer la conexión o el selector.
6. Implementar una subida real, verificar el original y registrar su referencia en Firestore antes de habilitar recepción.

No utilizar una cuenta de servicio como propietaria de archivos en un Drive personal. No dejar la carpeta pública. Las credenciales de prueba de Drive pueden expirar a los siete días; preparar el estado de publicación apropiado antes del evento.

## Primera autorización local (una cuenta)

La primera conexión se hace con connect_drive.py en la computadora del propietario, usando un receptor temporal limitado a loopback. No se publica una ruta OAuth sin protección en el backend. El receptor utiliza exactamente http://localhost:8765/ y se cierra al completar o cancelar.

En Google Auth Platform, configurar nombre La boda del siglo, audiencia externa en pruebas, correo del propietario como usuario de prueba y el permiso https://www.googleapis.com/auth/drive.file. Crear un cliente dedicado de tipo Aplicación web. Registrar http://localhost:8765/ como URI de redirección. Para la integración futura de Picker, registrar http://127.0.0.1:4200 y http://localhost:4200 como orígenes JavaScript. No modificar los clientes existentes de Firebase.

Descargar el JSON del nuevo cliente y conservarlo fuera del repositorio (por ejemplo, en Descargas). Ejecutar desde backend:

```powershell
.\.venv\Scripts\python.exe connect_drive.py --client-secrets "C:\ruta\al\cliente-descargado.json"
```

La cuenta debe autorizarse en el navegador por su propietario. El script no imprime la URL OAuth, códigos ni tokens. Guarda la conexión cifrada en .secrets/drive-credentials.enc y la clave en .env, ambos ignorados por Git. Conservar ambos privados; la recuperación de la conexión requiere ambos. Después seleccionar la carpeta con Picker y configurar Firebase antes de habilitar PHOTOS_ENABLED.

Pruebas locales: .\.venv\Scripts\python.exe -m unittest discover -s tests -v

## Autorizar la carpeta existente

Con la cuenta conectada, guardar GOOGLE_PICKER_API_KEY en .env. La clave debe pertenecer al mismo proyecto que OAuth, restringida a Google Picker API y a los sitios http://127.0.0.1:8766/*, http://localhost:8766/* y https://docs.google.com/*.

Ejecutar `.\.venv\Scripts\python.exe select_drive_folder.py`. Abre una página temporal en 127.0.0.1:8766, protegida con una sesión local de un solo uso. Elegir la carpeta configurada en GOOGLE_DRIVE_FOLDER_ID. El script verifica tipo de carpeta, que no esté en papelera y canAddChildren a través de Drive antes de guardar .secrets/drive-folder.json. No cambia permisos ni comparte la carpeta. La sesión caduca a los 15 minutos y el servidor se cierra al verificar la selección.

Picker recibe un token de acceso temporal dentro de esta configuración local; el token de renovación y secreto OAuth permanecen cifrados fuera del navegador. Nunca desplegar select_drive_folder.py ni su HTML como parte de la web pública. Docker solo copia app/. Esta selección autoriza el destino; la recepción depende además de la conexión Firebase y PHOTOS_ENABLED.
