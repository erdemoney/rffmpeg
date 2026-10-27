# Set up rffmpeg with Jellyfin

This guide focuses on a LinuxServer Jellyfin container and one Linux transcode worker. The worker can be a small server or another machine with supported hardware acceleration. `rffmpeg` runs inside the Jellyfin container and starts FFmpeg on the worker over SSH.

```text
Jellyfin container  -- SSH -->  transcode worker
       |                            |
       +------ shared paths --------+
```

The important requirement is that every path passed to FFmpeg (media, transcode output, and any temporary files) is available at the **same absolute path** inside the Jellyfin container and on the worker.

## 1. Choose the shared paths

The example below makes media available at `/data` and the Jellyfin cache at `/cache` inside the container:

```yaml
volumes:
  - /srv/jellyfin/config:/config
  - /srv/jellyfin/cache:/cache
  - /srv/media:/data:ro
```

Mount the same cache and media directories on the worker at `/cache` and `/data`. These paths may be provided by NFS, SMB, CephFS, or another shared filesystem. The worker does not need to run in Docker.

**Do not share `/config` wholesale with the worker.** The LinuxServer rffmpeg mod stores its SSH private key and rffmpeg database under `/config/rffmpeg`. If FFmpeg logs show that Jellyfin uses another path under `/config` (for example, for subtitle files), share only that required subdirectory and keep `/config/rffmpeg` private.

For Jellyfin 10.10 and newer, set `TMPDIR` to a directory on a shared path, such as `/cache/temp`, and create it with permissions for the Jellyfin container user. FFmpeg input and output paths still need to match on both machines.

## 2. Prepare the worker

On the worker:

1. Install an SSH server and the same Jellyfin FFmpeg version used by the container. The worker does not need Jellyfin or rffmpeg installed.
2. Create the SSH account named by `RFFMPEG_USER`. Give it the same numeric UID and GID as the container's `PUID` and `PGID`, so it can read media and write transcode files on shared storage.
3. Mount the shared paths at the same locations (`/data`, `/cache`, and any additional required paths).
4. If using hardware acceleration, install the necessary drivers and give the SSH account access to the relevant devices/groups.

On the Jellyfin host, find the IDs you plan to use with `id <user>`. On the worker, `id <RFFMPEG_USER>` should show matching numeric IDs.

## 3. Enable the LinuxServer rffmpeg Docker Mod

The [LinuxServer rffmpeg Docker Mod](https://github.com/linuxserver/docker-mods/tree/jellyfin-rffmpeg) installs the wrapper, aliases, and Python dependencies in the container. Add it to your Compose service:

```yaml
services:
  jellyfin:
    image: lscr.io/linuxserver/jellyfin:latest
    container_name: jellyfin
    environment:
      PUID: "1000"                 # use the IDs chosen above
      PGID: "1000"
      TZ: Etc/UTC
      DOCKER_MODS: linuxserver/mods:jellyfin-rffmpeg
      RFFMPEG_USER: jellyfin       # SSH account on the worker
      RFFMPEG_HOST: transcode1     # worker hostname or IP
      TMPDIR: /cache/temp          # Jellyfin 10.10+
    volumes:
      - /srv/jellyfin/config:/config
      - /srv/jellyfin/cache:/cache
      - /srv/media:/data:ro
    restart: unless-stopped
```

If you use other LinuxServer mods, separate them with `|` in `DOCKER_MODS`. The rffmpeg mod creates its config and initializes the database on first startup; `RFFMPEG_HOST` adds the initial worker. These environment values are applied during initial setup, so use the rffmpeg CLI to add or change workers later.

The mod's default FFmpeg path uses its wake-on-LAN wrapper. Set `FFMPEG_PATH=/usr/local/bin/ffmpeg` in Compose to bypass that wrapper. Follow the mod's current instructions for configuring Jellyfin to use the selected FFmpeg path.

## 4. Install the SSH key

The mod expects the private key at `/config/rffmpeg/.ssh/id_rsa` by default. The `/config` volume persists this file across container recreations. For a new installation, create the key before the first container start; the mod creates an empty placeholder if it does not find one.

1. Create an SSH keypair at that path on the Jellyfin host, owned by the UID/GID used for the container. Keep the private key mode at `600` and its directory at `700`.
2. Add the `.pub` key to the `authorized_keys` file for `RFFMPEG_USER` on the worker.
3. If you use a different key filename, update `remote.args` in `/config/rffmpeg/rffmpeg.yml` to point to it.

For example, on the Jellyfin host (use your actual config path and run as the `PUID` user):

```bash
install -d -m 700 /srv/jellyfin/config/rffmpeg/.ssh
ssh-keygen -t rsa -b 4096 -N "" -f /srv/jellyfin/config/rffmpeg/.ssh/id_rsa
```

Then install the public key for the worker account:

```bash
ssh-copy-id -i /srv/jellyfin/config/rffmpeg/.ssh/id_rsa.pub jellyfin@transcode1
```

Keep the private key and `/config/rffmpeg` off the worker's shared mounts.
Once the key is installed and the Compose configuration is ready, start or recreate the Jellyfin container.

## 5. Check the setup

After the container starts, run the preflight check as its `abc` user. Include the shared paths to check:

```bash
docker exec --user abc jellyfin /usr/local/bin/rffmpeg check \
  --shared-read /data \
  --shared-write /cache
```

The check verifies the `ffmpeg` and `ffprobe` aliases, database and worker configuration, FFmpeg/ffprobe availability on each worker, and optional path access. It checks that directories exist and have the requested permissions; it cannot confirm that local and worker paths point to the same backing storage. It does not modify configuration, apply migrations, or initialize the database. If it reports an error, fix that item and run it again.

For a manual SSH test:

```bash
docker exec --user abc jellyfin ssh -i /config/rffmpeg/.ssh/id_rsa jellyfin@transcode1 uname -a
```

To inspect worker registration and active jobs:

```bash
docker exec --user abc jellyfin /usr/local/bin/rffmpeg status
```

**Do not run `rffmpeg init` on an existing installation.** It recreates the database and removes the registered workers and state. The LinuxServer mod initializes a new database automatically when one is not present.

## 6. Configure Jellyfin and test playback

Make sure Jellyfin uses the FFmpeg path selected in Compose/mod settings. Then play media that requires transcoding and check the rffmpeg log:

```bash
docker exec --user abc jellyfin /usr/local/bin/rffmpeg log -f
```

The log should show a worker selected and the command completing. If playback fails, compare the paths in the FFmpeg command with the paths mounted on the worker, and run `rffmpeg check` again with those paths.

## Shared storage example: NFS

NFS is one way to provide the shared paths; it is not required. Export only the cache, media, and other directories FFmpeg needs, and restrict exports to the worker's address. Mount them on the worker at the same absolute paths used in the container. Keep `/config`, the rffmpeg database, and SSH private keys out of those exports. Use the same UID/GID on both sides; do not copy `no_root_squash` into an export unless you have a specific, understood need for it.

For example, on the Jellyfin host, export the host directories used by the Compose mounts. Replace `WORKER_IP` with the worker's address:

```bash
sudo apt install nfs-kernel-server
sudo tee /etc/exports.d/jellyfin-rffmpeg.exports >/dev/null <<'EOF'
/srv/jellyfin/cache WORKER_IP(rw,sync,no_subtree_check)
/srv/media          WORKER_IP(ro,sync,no_subtree_check)
EOF
sudo exportfs -ra
```

On the worker, mount those exports at the paths FFmpeg sees inside the container:

```bash
sudo apt install nfs-common
sudo mkdir -p /cache /data
sudo mount -t nfs4 JELLYFIN_HOST:/srv/jellyfin/cache /cache
sudo mount -t nfs4 JELLYFIN_HOST:/srv/media /data
```

Add equivalent entries to the worker's `/etc/fstab` if the mounts should persist across reboots. The cache must be writable by the matching service UID/GID; media can remain read-only.

For NFS delays when Jellyfin checks newly generated segments, use an appropriate short attribute-cache timeout for the transcode mount. The exact mount/export options depend on your storage and network; verify the worker can read media and write transcodes before configuring Jellyfin to use rffmpeg.

## Other installation types

### Official Jellyfin Docker image

The worker, SSH, and shared-path requirements above are the same. The LinuxServer Docker Mod does not apply to the official image. Build a custom image that includes the rffmpeg script, Python 3, Click, PyYAML, OpenSSH client, and `ffmpeg`/`ffprobe` symlinks; persist the rffmpeg config, key, and database in a private config volume. Do not install into a running container, since those changes are lost when it is recreated.

### Native Debian/Ubuntu installation

Install the dependencies and wrapper on the Jellyfin host, then create the two aliases:

```bash
sudo apt install python3-click python3-yaml openssh-client
sudo install -m 755 rffmpeg /usr/local/bin/rffmpeg
sudo ln -s rffmpeg /usr/local/bin/ffmpeg
sudo ln -s rffmpeg /usr/local/bin/ffprobe
sudo install -d -m 755 /etc/rffmpeg
sudo cp -n rffmpeg.yml.sample /etc/rffmpeg/rffmpeg.yml
```

Set `rffmpeg.remote.user` and the SSH key path in `/etc/rffmpeg/rffmpeg.yml`. `subprocess` is included with Python and does not need a separate package. Install `python3-psycopg2` only if using PostgreSQL. On a new install, initialize once and register the worker:

```bash
sudo rffmpeg init
sudo -u jellyfin rffmpeg add transcode1
```

`init` resets the database if run again; do not use it to update an existing setup.

## Optional: hardware acceleration and hardening

Set up hardware acceleration only after basic remote transcoding works. The same acceleration mode and compatible FFmpeg build must be available wherever FFmpeg may run. See the [hardware acceleration notes](../README.md#hardware-acceleration).

The separate [hardening guide](HARDENING) is an advanced, post-install step. First verify that normal SSH access and transcoding work.
