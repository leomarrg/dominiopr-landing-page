# DOMINIO — Deployment Guide

Deployment a AWS Lightsail con dominio en GoDaddy y CI/CD vía GitHub Actions.

---

## Arquitectura

```
GoDaddy DNS  ───────────►  AWS Lightsail Instance (Ubuntu 22.04)
   *.dominiopr.com               │
                                 ├── Nginx (reverse proxy + SSL)
                                 │     ├── dominiopr.com         → gunicorn-dominio.sock
                                 │     ├── registratepr...com    → gunicorn-registratepr.sock  (futuro)
                                 │     ├── linkea...com          → gunicorn-linkea.sock        (futuro)
                                 │     └── pulsopolitico...com   → gunicorn-pulsopolitico.sock (futuro)
                                 │
                                 ├── Gunicorn (Django, vía systemd)
                                 ├── SQLite (db local)
                                 └── Let's Encrypt (SSL gratis, renueva auto)

GitHub Actions: on push to main → SSH al servidor → ./deploy/scripts/deploy.sh
```

**Costo estimado**: ~$10-12/mes (Lightsail $5 plan basta para arrancar; sube a $10 si necesitas más RAM).

---

## Parte 1 — Una sola vez: configurar el servidor

### 1.1 Crear la instancia en Lightsail

1. Abre [lightsail.aws.amazon.com](https://lightsail.aws.amazon.com/) y haz login.
2. **Create instance**:
   - Región: **us-east-1** (Virginia) — más cercana a PR.
   - Platform: **Linux/Unix**
   - Blueprint: **OS Only → Ubuntu 22.04 LTS**
   - Plan: **$5/mes** (1 vCPU, 1 GB RAM, 40 GB SSD) — suficiente para landing + 3 productos pequeños.
   - Identifier: `dominio-prod`
3. **Create instance** y espera ~2 min.
4. Una vez running:
   - Tab **Networking** → **Create static IP** → adjúntala a la instancia. Anótala (ej. `54.123.45.67`).
   - Tab **Networking** → **Firewall** → abre puertos:
     - SSH (22) — ya viene abierto
     - HTTP (80)
     - HTTPS (443)

### 1.2 Apuntar el dominio en GoDaddy

1. Login en GoDaddy → **My Products** → **DNS** del dominio `dominiopr.com`.
2. Borra los registros A/CNAME existentes que apunten al parking page de GoDaddy.
3. Añade estos registros (TTL: 600):

   | Tipo  | Nombre | Valor                          |
   |-------|--------|--------------------------------|
   | A     | @      | `<IP-estática-de-Lightsail>`   |
   | A     | www    | `<IP-estática-de-Lightsail>`   |
   | A     | *      | `<IP-estática-de-Lightsail>`   |

   El `*` cubre **todos los subdominios futuros** (registratepr, linkea, pulsopolitico). Cuando añadas un producto, no tienes que tocar GoDaddy.

4. Espera 5-30 min para propagación. Verifica con: `nslookup dominiopr.com` debe devolver la IP de Lightsail.

### 1.3 Conectarte a la instancia por SSH

1. En Lightsail → tu instancia → **Connect** tab → **Download default key** (`LightsailDefaultKey-us-east-1.pem`). Guárdala en `~/.ssh/lightsail-dominio.pem`.
2. En tu terminal local:
   ```bash
   chmod 400 ~/.ssh/lightsail-dominio.pem
   ssh -i ~/.ssh/lightsail-dominio.pem ubuntu@<IP-estática>
   ```

### 1.4 Setup inicial del servidor

Una vez dentro por SSH, corre estos comandos (cópialos todos juntos):

```bash
# Update + paquetes base
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git nginx certbot python3-certbot-nginx

# Crear directorio de la app
sudo mkdir -p /var/www/dominio
sudo chown ubuntu:ubuntu /var/www/dominio

# Clonar el repo
cd /var/www
git clone https://github.com/<TU-USUARIO>/dominiopr-landing-page.git dominio
cd dominio

# Crear venv e instalar deps
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Crear el .env de producción
cp .env.example .env
nano .env   # edita: pon un SECRET_KEY nuevo, DJANGO_DEBUG=False
# Genera SECRET_KEY así (en otra ventana o después):
# python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"

# Migrate + collectstatic
python manage.py migrate
python manage.py collectstatic --noinput
deactivate
```

### 1.5 Configurar Gunicorn (systemd)

```bash
# Copiar el service file
sudo cp /var/www/dominio/deploy/systemd/gunicorn-dominio.service /etc/systemd/system/

# Habilitar y arrancar
sudo systemctl daemon-reload
sudo systemctl enable gunicorn-dominio
sudo systemctl start gunicorn-dominio

# Verificar
sudo systemctl status gunicorn-dominio
# Debe decir "active (running)". Si no, revisa logs: sudo journalctl -u gunicorn-dominio -n 50
```

### 1.6 Configurar Nginx

```bash
# Quitar el default
sudo rm /etc/nginx/sites-enabled/default

# Copiar nuestro config
sudo cp /var/www/dominio/deploy/nginx/dominio.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/dominio.conf /etc/nginx/sites-enabled/

# IMPORTANTE: el config referencia certs de Let's Encrypt que aún no existen.
# Comenta temporalmente las líneas de SSL antes de probar (o salta al paso 1.7 primero).
```

### 1.7 SSL gratis con Let's Encrypt

Antes de hacer esto, **el DNS debe estar propagado** (paso 1.2). Verifica:
```bash
dig +short dominiopr.com  # debe devolver tu IP de Lightsail
```

Luego en el servidor:

```bash
# Genera cert para dominio raíz + www
sudo certbot --nginx -d dominiopr.com -d www.dominiopr.com

# Sigue el prompt: pon tu email, acepta TOS, elige opción 2 (redirect HTTP→HTTPS)
```

Certbot edita el Nginx config automáticamente y crea los certs. **Renovación auto** ya viene configurada vía cron.

Para subdominios futuros (cuando añadas un producto), repites:
```bash
sudo certbot --nginx -d registratepr.dominiopr.com
```

### 1.8 Verificar

Abre en el navegador: `https://dominiopr.com` — debe cargar tu landing con HTTPS verde.

Si algo falla:
- `sudo systemctl status gunicorn-dominio` — Gunicorn corriendo?
- `sudo nginx -t` — config válida?
- `sudo journalctl -u gunicorn-dominio -f` — errores de Django
- `sudo tail -f /var/log/nginx/error.log` — errores de Nginx

---

## Parte 2 — Una sola vez: configurar GitHub Actions

### 2.1 Generar un SSH key dedicado para CI/CD

**En tu máquina local** (no en el servidor):

```bash
ssh-keygen -t ed25519 -f ~/.ssh/dominio-deploy -N "" -C "github-actions@dominio"
```

Esto crea `~/.ssh/dominio-deploy` (privada) y `~/.ssh/dominio-deploy.pub` (pública).

### 2.2 Autorizar la key en el servidor

Copia la pública al servidor:

```bash
cat ~/.ssh/dominio-deploy.pub
# Copia el output
```

En el servidor:

```bash
ssh -i ~/.ssh/lightsail-dominio.pem ubuntu@<IP>
echo "<contenido-de-la-pub-key>" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

### 2.3 Dar permiso a `ubuntu` de reiniciar Gunicorn sin sudo password

El script de deploy hace `sudo systemctl restart gunicorn-dominio`. Para que no pida password en CI:

```bash
sudo visudo -f /etc/sudoers.d/dominio-deploy
```

Añade esta línea:
```
ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart gunicorn-dominio
```

Guarda (`Ctrl+O`, Enter, `Ctrl+X`).

### 2.4 Hacer el deploy script ejecutable

```bash
chmod +x /var/www/dominio/deploy/scripts/deploy.sh
```

### 2.5 Añadir secrets en GitHub

Ve a tu repo en GitHub → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**.

Crea estos 3 secrets:

| Nombre               | Valor                                                    |
|----------------------|----------------------------------------------------------|
| `LIGHTSAIL_HOST`     | IP estática de Lightsail (ej. `54.123.45.67`)            |
| `LIGHTSAIL_USER`     | `ubuntu`                                                 |
| `LIGHTSAIL_SSH_KEY`  | Contenido COMPLETO de `~/.ssh/dominio-deploy` (privada)  |

Para copiar la private key correctamente:
```bash
cat ~/.ssh/dominio-deploy
# Copia desde -----BEGIN OPENSSH PRIVATE KEY----- hasta -----END OPENSSH PRIVATE KEY----- INCLUIDOS
```

### 2.6 Probar el workflow

Haz un commit dummy y push a `main`:
```bash
git commit --allow-empty -m "test: trigger first deploy"
git push origin main
```

Ve a tu repo → **Actions** tab → debe correr el workflow "Deploy to AWS Lightsail". Si pasa verde, todo está conectado.

A partir de aquí: **push a `main` = deploy automático**.

---

## Parte 3 — Añadir un producto nuevo (cuando exista)

Cuando RegístratePR / Linkea / Pulso Político estén listos:

1. **Si el producto es Django**: crea su propio repo (por ejemplo `registratepr`).
2. En el servidor:
   ```bash
   cd /var/www
   git clone https://github.com/<usuario>/registratepr.git
   cd registratepr
   python3 -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env  # edita
   python manage.py migrate
   python manage.py collectstatic --noinput
   ```
3. **Service file**: copia `deploy/systemd/gunicorn-dominio.service` como modelo y renombra a `gunicorn-registratepr.service`. Ajusta `WorkingDirectory`, paths, y `--bind unix:/run/gunicorn-registratepr.sock`. Habilita con `systemctl enable/start`.
4. **Nginx**: en `/etc/nginx/sites-available/dominio.conf` descomenta el bloque template (al final del archivo) y ajusta `registratepr` por el nombre del producto.
5. **SSL**: `sudo certbot --nginx -d registratepr.dominiopr.com`
6. **GitHub Actions**: cada producto tiene su propio repo → su propio `.github/workflows/deploy.yml` apuntando al mismo servidor pero con su propio path/servicio.

El DNS wildcard `*.dominiopr.com` ya cubre cualquier subdominio nuevo — no tocas GoDaddy.

---

## Operaciones del día a día

| Acción                         | Comando                                            |
|--------------------------------|----------------------------------------------------|
| Ver logs de la landing         | `sudo journalctl -u gunicorn-dominio -f`           |
| Restart manual                 | `sudo systemctl restart gunicorn-dominio`          |
| Status                         | `sudo systemctl status gunicorn-dominio`           |
| Forzar deploy manual           | `bash /var/www/dominio/deploy/scripts/deploy.sh`   |
| Renovar SSL manualmente        | `sudo certbot renew`                               |
| Re-cargar Nginx tras editar    | `sudo nginx -t && sudo systemctl reload nginx`     |
| Backup SQLite                  | `cp /var/www/dominio/db.sqlite3 ~/backup-$(date +%F).sqlite3` |

---

## Troubleshooting rápido

- **502 Bad Gateway** → Gunicorn cayó. `sudo systemctl status gunicorn-dominio` + journalctl.
- **CSS no carga (404)** → Faltó `collectstatic`. Corre `python manage.py collectstatic --noinput` en el servidor.
- **CSRF errors al enviar el form** → revisa `DJANGO_CSRF_TRUSTED_ORIGINS` en `.env` incluye el dominio HTTPS.
- **SSL expirado** → certbot debería renovar solo. Verifica con `sudo certbot renew --dry-run`.
- **El workflow de GitHub falla** → revisa los Actions logs; el problema más común es el SSH key mal pegado en secrets (debe incluir las líneas BEGIN/END).
