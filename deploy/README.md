<!-- This document uses ASD-STE100 Simplified Technical English.
     Keep the sentences short. Use the active voice. Give one
     instruction in one sentence. Keep procedures and descriptions
     apart. -->

# estonian-mcp with nginx

## 1 General

This directory contains a Docker Compose configuration. The
configuration starts three containers.

| Container | Function |
| --- | --- |
| `app` | The estonian-mcp server. |
| `nginx` | The proxy. It does the client authentication and the rate control. |
| `certbot` | The Let's Encrypt client. It gets and renews the certificate. |

The data flow is as follows:

```
client --- 443 ---> nginx ---> app
                      ^
certbot --------------+
```

The `app` container connects only to the `edge` network. Only nginx
connects to that network. You cannot connect to the `app` container
from the host.

## 2 Why nginx does the authentication

The `server.py` file has its own bearer authentication. This
installation keeps that function. But nginx authenticates the clients,
and the app token protects only the connection between nginx and the
app.

nginx gives these advantages:

- nginx stops bad requests before they get to Python. This includes TLS
  errors, incorrect HTTP data, slow clients and too many connections.
- The app accepts only one token. nginx accepts a different token for
  each client.
- Each client has its own name in the log and its own rate limit.
- To remove a client, delete one line and reload nginx.
- The client token does not go into the Python process.

nginx does not protect the tool calls. EstNLTK processes each tool call
as before. nginx makes the edge safe, but not the core.

## 3 Prerequisites

Make sure that these conditions are correct before you start:

1. The host name in `DOMAIN` points to this host. An A record is
   necessary. An AAAA record is also necessary if the host has an IPv6
   address.
2. Port 80 is open to the internet.
3. Port 443 is open to the internet.

> **CAUTION: Keep port 80 open after the installation. certbot renews
> the certificate through port 80. If you close port 80, the
> certificate becomes invalid after a maximum of 90 days.**

## 4 Installation

### 4.1 Procedure

1. Copy the example configuration:

   ```sh
   cp .env.example .env
   ```

2. Edit `.env`. Set `DOMAIN`, `LETSENCRYPT_EMAIL` and `INTERNAL_TOKEN`.
3. Make the first client token:

   ```sh
   ./deploy/new-token.sh my-laptop
   ```

   The script makes the `tokens.map` file. It shows the new token.
   Record the token now. The `tokens.map` file also contains the token.
4. Start the installation script:

   ```sh
   ./deploy/init-letsencrypt.sh
   ```

   The script gets the certificate. Then it starts the three
   containers.

> **NOTE: If you use Git Bash on Windows, put `MSYS_NO_PATHCONV=1`
> before the command in step 4. Git Bash changes the container paths
> and the `-subj "/CN=..."` argument into Windows paths. Then the
> script fails.**

### 4.2 Test mode

The `.env` file contains `STAGING=1`. In this mode, Let's Encrypt gives
a test certificate. A browser does not accept a test certificate.

Use the test mode until the DNS and the firewall are correct. The
production server permits only 5 incorrect tests for each host name in
one hour.

To get a production certificate, do these steps:

1. Make sure that this command gives an answer from a different host:

   ```sh
   curl http://$DOMAIN/health
   ```

2. Set `STAGING=0` in `.env`.
3. Start the installation script again:

   ```sh
   ./deploy/init-letsencrypt.sh
   ```

### 4.3 Check of the installation

Do these three commands. The table shows the correct results.

```sh
curl https://$DOMAIN/health
curl -i https://$DOMAIN/mcp
curl -i -H "Authorization: Bearer $TOKEN" https://$DOMAIN/mcp
```

| Command | Correct result |
| --- | --- |
| 1 | Status 200. The body shows the version. |
| 2 | Status 401. |
| 3 | Status 200. |

### 4.4 Connection of a client

Use this command to connect Claude Code to the server:

```sh
claude mcp add --transport http estonian https://$DOMAIN/mcp \
  --header "Authorization: Bearer $TOKEN"
```

## 5 Ports and local tests

### 5.1 Ports

nginx always listens on port 80 and port 443 in the container. The
`.env` file sets the ports on the host.

| Variable | Default | Function |
| --- | --- | --- |
| `HTTP_PORT` | 80 | The host port for HTTP and for the ACME challenge. |
| `HTTPS_PORT` | 443 | The host port for HTTPS. |

Change these ports for local tests. Port 80 and port 443 are usually
already in use on a workstation.

nginx also puts `HTTPS_PORT` in the redirect from HTTP to HTTPS. Do not
change the port in only one place. If the two values are different, the
redirect points to a port with no server.

> **CAUTION: Do not change `HTTP_PORT` on a public host. Let's Encrypt
> always connects to the public port 80. If you change the port, the
> validation fails.**

### 5.2 Local tests

Let's Encrypt cannot give a certificate for a local host. Use a
self-signed certificate for local tests.

1. Set `HTTP_PORT` and `HTTPS_PORT` in `.env` to free ports.
2. Make the certificate and start the stack:

   ```sh
   ./deploy/local-cert.sh
   ```

3. Do a test. Use the `-k` option, because the certificate is
   self-signed:

   ```sh
   curl -k https://localhost:8443/health
   curl -ki -H "Authorization: Bearer $TOKEN" https://localhost:8443/mcp
   ```

> **CAUTION: Do not use a self-signed certificate on a public host.**

## 6 Client tokens

### 6.1 To add a client

1. Make the token:

   ```sh
   ./deploy/new-token.sh partner-acme
   ```

2. Reload nginx:

   ```sh
   docker compose exec nginx nginx -s reload
   ```

### 6.2 To remove a client

1. Delete the line for that client from `deploy/nginx/secrets/tokens.map`.
2. Reload nginx.

To change a token, remove the client and then add the client again. A
change of a token does not need a restart. It does not change the app.

### 6.3 Token security

The `tokens.map` file contains the tokens in plain text. Git does not
store this file.

nginx compares the tokens with a hash function. This is not a
constant-time comparison. But each token has 40 random characters, and
thus a remote timing attack is not possible in practice. Keep the file
secret.

## 7 Certificate renewal

The `certbot` container makes a check two times each day. It renews the
certificate 30 days before the expiry date.

nginx reloads four times each day to read the new certificate files.
certbot cannot send a signal to a different container. To do this,
certbot must have the Docker socket. This is not a safe procedure, and
thus the reload uses a timer.

### 7.1 To show the certificate status

```sh
docker compose exec certbot certbot certificates
docker compose logs certbot
```

### 7.2 To renew the certificate immediately

1. Start the renewal:

   ```sh
   docker compose run --rm --entrypoint certbot certbot \
     renew --webroot -w /var/www/certbot --force-renewal
   ```

2. Reload nginx:

   ```sh
   docker compose exec nginx nginx -s reload
   ```

## 8 Changes to the limits

The limits are in
`deploy/nginx/templates/mcp.conf.template`.

| Limit | Default | Directive |
| --- | --- | --- |
| Requests for each client | 120 in one minute | `limit_req_zone ... zone=per_client` |
| Requests for each IP address | 300 in one minute | `limit_req_zone ... zone=per_ip` |
| Open streams for each client | 8 | `limit_conn conn_client` |
| Maximum size of a request | 4 MB | `client_max_body_size` |

### 8.1 To apply a change

```sh
docker compose up -d --force-recreate nginx
```

> **CAUTION: A reload is not sufficient. The container makes the
> configuration file from the template when it starts. A reload does
> not make the file again.**

## 9 Notes

- The `/health` and `/metrics` paths stay open to all clients. This is
  the same behaviour as the `server.py` file. To close `/metrics`, add
  `allow` and `deny` directives to its location block.
- The rate limit of the app is very high
  (`ESTNLTK_MCP_RATE_LIMIT_PER_MINUTE=100000`). This is necessary. All
  requests come to the app with the same internal token. Thus the app
  counts all the clients as one group. nginx does the rate control.
- The Smithery `?config=` token does not operate behind this proxy.
  nginx reads only the `Authorization` header. Registry discovery
  through `/.well-known/mcp/server-card.json` continues to operate.
- nginx sends the client name to the app in the `X-MCP-Client` header.
  The app does not read this header at this time. The header lets the
  app identify a client without a token.
- nginx finds the address of the app for each request. It does not find
  the address one time when it starts. Thus nginx starts even if the
  app container is down. This is important, because certbot cannot
  renew the certificate if nginx is down.
- nginx mounts the `deploy/nginx/secrets` directory. It does not mount
  the `tokens.map` file. A bind mount of one file has two faults.
  Docker makes a directory if the file does not exist. An editor that
  replaces the file also changes the inode, and the container then
  continues to read the old data.
- nginx reads all the files that agree with `tokens*.map` in the
  secrets directory. You can put the clients in more than one file.
- Python continues to supply the start page and the icons. You can move
  these files to nginx. Do this only if these requests become a large
  part of the traffic.

## 10 Troubleshooting

### 10.1 The app cannot write the metrics file

The log shows this message:

```
WARNING  metrics persistence: failed to save /data/metrics.json:
         [Errno 13] Permission denied: '/data/metrics.json.tmp'
```

Docker made the `metrics` volume before the image contained the `/data`
directory. Thus the volume belongs to the user `root`. But the app
operates as the user `app`, and it cannot write to the volume.

Docker sets the owner of a volume only one time, when it makes the
volume. A new image alone does not correct an old volume. You must
delete the volume.

> **CAUTION: This procedure deletes the metrics counters. There is no
> other data in this volume.**

1. Stop the stack:

   ```sh
   docker compose down
   ```

2. Delete the volume:

   ```sh
   docker volume rm estonian-mcp_metrics
   ```

3. Build the app image again:

   ```sh
   docker compose build app
   ```

4. Start the stack:

   ```sh
   docker compose up -d
   ```

### 10.2 nginx does not start and shows "not a directory"

The error message contains this text:

```
not a directory: Are you trying to mount a directory onto a file
(or vice-versa)?
```

Docker makes a directory when a bind mount source does not exist. Thus
a missing `tokens.map` file becomes a directory, and Docker cannot then
mount that directory on to a file.

The stack mounts the `deploy/nginx/secrets` directory to prevent this.
If you see this error, an old configuration is still in use.

1. Stop the stack:

   ```sh
   docker compose down
   ```

2. Delete `deploy/nginx/tokens.map` if it is a directory.
3. Make a token:

   ```sh
   ./deploy/new-token.sh my-laptop
   ```

4. Start the stack again:

   ```sh
   docker compose up -d
   ```

### 10.3 All clients get status 401

nginx starts even if it finds no token file. In this condition, nginx
refuses all clients. This is intentional. If nginx stops, the ACME
challenge on port 80 also stops, and certbot cannot renew the
certificate.

1. Make sure that `deploy/nginx/secrets/tokens.map` exists.
2. Make sure that the file name starts with `tokens` and ends with
   `.map`. nginx reads only these files.
3. Reload nginx.

## 11 Reference

- Server code and the internal authentication: [`server.py`](../server.py)
- Compose configuration: [`docker-compose.yaml`](../docker-compose.yaml)
- nginx configuration: [`mcp.conf.template`](nginx/templates/mcp.conf.template)
- Port suffix for the redirect: [`https-port-suffix.envsh`](nginx/https-port-suffix.envsh)

Scripts:

| Script | Function |
| --- | --- |
| [`init-letsencrypt.sh`](init-letsencrypt.sh) | Gets the first Let's Encrypt certificate. |
| [`local-cert.sh`](local-cert.sh) | Makes a self-signed certificate for local tests. |
| [`new-token.sh`](new-token.sh) | Makes a client token. |
