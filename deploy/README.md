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

The `app` container connects only to the `edge` network. It is not
published to the host.

> **NOTE: "Not published" is not the same as "not reachable". On Linux
> the bridge network of the container is routable from the host itself.
> The `app` container thus keeps its own bearer authentication. The
> `INTERNAL_TOKEN` value protects that connection.**

## 2 Why nginx does the authentication

The `server.py` file has its own bearer authentication. This
installation keeps that function. But nginx authenticates the clients,
and the app token protects only the connection between nginx and the
app.

nginx gives these advantages:

- nginx stops bad requests before they get to Python. This includes TLS
  errors, incorrect HTTP data, slow clients and too many connections.
- The rate limit applies to the requests that nginx refuses, and not
  only to the requests that it accepts. Thus a flood of incorrect
  tokens, incorrect methods or unknown paths is also controlled.
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

curl -i -X POST https://$DOMAIN/mcp

curl -i -X POST https://$DOMAIN/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

| Command | Correct result |
| --- | --- |
| 1 | Status 200. The body shows the version. |
| 2 | Status 401. |
| 3 | Status 200. The body is a JSON-RPC result. |

> **NOTE: The MCP endpoint needs both headers in command 3. Without the
> `Accept` header the server answers 406, and without the
> `Content-Type` header it answers 400. Neither is an authentication
> fault: a wrong token gives 401.**

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

## 6 Clients

### 6.1 The two types of client

Each client has a name. The name is its identity: it shows in the
access log, it has its own rate limit, and you can revoke it alone.

There are two types. Use the correct script for the type.

| Type | Example | Script |
| --- | --- | --- |
| Sends an `Authorization` header | Claude Code, curl | `new-token.sh` |
| Cannot send a header, uses OAuth | Claude custom connector | `new-oauth-client.sh` |

An OAuth client is a normal client after the OAuth steps. It gets its
own access token. Thus the log, the rate limit and the revocation
operate the same for both types.

### 6.2 To add a client

1. Make the credentials. Use one of these two commands:

   ```sh
   ./deploy/new-token.sh partner-acme
   ./deploy/new-oauth-client.sh partner-acme
   ```

2. Reload nginx:

   ```sh
   docker compose exec nginx nginx -s reload
   ```

### 6.3 To remove a client

1. Revoke the credentials:

   ```sh
   ./deploy/revoke-client.sh partner-acme
   ```

2. Reload nginx.

A client can have credentials in three files. The script removes all of
them. It also examines each additional file that agrees with
`tokens*.map`, `oauth_tokens*.map` or `oauth_secrets*.map`, because
nginx reads all of these. Do not delete the lines by hand: one file that
you forget leaves a usable way in.

> **CAUTION: The old credentials continue to operate until you reload
> nginx.**

To change the credentials, revoke the client and then add the client
again. This does not need a restart, and it does not change the app.

### 6.4 Token security

The `tokens.map` file contains the tokens in plain text. Git does not
store this file. The scripts make the file with mode 0600 and the
directory with mode 0700, thus other accounts on the host cannot read
the tokens. nginx reads its configuration as root and is not affected.

nginx compares the tokens with a hash function. This is not a
constant-time comparison. But each token has 40 random characters, and
thus a remote timing attack is not possible in practice. Keep the file
secret.

nginx makes the comparison in lower case. The match is thus
case-insensitive. A token of 40 random alphanumeric characters keeps
approximately 207 bits after this. Do not make a token by hand that
uses capital letters for its strength.

## 7 OAuth for the Claude connector

### 7.1 General

Some clients cannot send a static `Authorization` header. The Claude
custom connector is one of them. It does OAuth discovery instead.

nginx answers that discovery with a facade. The facade does not make
users or sessions. It gives each OAuth client its own access token, and
that token is also in `tokens.map`. Thus `/mcp` accepts it through the
usual bearer path, and the client keeps its own name and its own rate
limit.

This is the sequence from a real connection attempt:

| Step | Request | Answer |
| --- | --- | --- |
| 1 | `POST /mcp` | 401 with `resource_metadata` |
| 2 | `GET /.well-known/oauth-protected-resource/mcp` | The resource document |
| 3 | `GET /.well-known/oauth-protected-resource` | The same document |
| 4 | `GET /.well-known/oauth-authorization-server` | The endpoint list |
| 5 | `GET /oauth/authorize` in a browser | A redirect with a single-use code |
| 6 | `POST /oauth/token` with the client secret | The access token |

The client also tries `POST /register` for Dynamic Client Registration.
The server answers 404. This is correct. A registration endpoint must
give a client secret to each caller, and that secret is the only
protection the token endpoint has. You must give the client id and the
client secret by hand.

### 7.2 The three credentials

The script makes three credentials for each OAuth client. They operate
together.

| Credential | File | Function |
| --- | --- | --- |
| Client secret | `oauth_secrets.map` | nginx checks it at `/oauth/token`. This is the only real protection. |
| Access token | `oauth_tokens.map` | The token that `/oauth/token` gives to this client. |
| Bearer token | `tokens.map` | The same token, which `/mcp` checks. |

You do not copy the access token anywhere. Only the client id and the
client secret go into the connector.

`oauth_secrets.map` holds the SHA-256 of the secret, not the secret. If
you lose the secret, make the client again.

### 7.3 How nginx reads the client secret

Claude sends the client secret in the request body, not in an
`Authorization` header. nginx cannot read a request body. Thus the
token endpoint uses njs, which is the JavaScript engine of nginx.

njs is not Node. It is a module from the nginx team, and it operates in
the nginx worker process. There is no other program and no other
container.

The file is [`njs/oauth.js`](nginx/njs/oauth.js). It reads the
credentials, compares the digest of the secret, and gives back the
access token of that client. It accepts a header too, thus `curl` and
other clients continue to operate.

The same file makes the authorization codes. Each code is a random
value from nginx. nginx keeps the code in a shared memory zone with the
client id, the callback address and the PKCE challenge of that request.
The code is valid one time and for 10 minutes. A restart of the
container removes the codes. The client then does the authorization
again.

### 7.4 To set up OAuth

1. Make the client:

   ```sh
   ./deploy/new-oauth-client.sh my-connector
   ```

2. Reload nginx:

   ```sh
   docker compose exec nginx nginx -s reload
   ```

3. In the connector, open **Advanced settings**. Put the client id in
   **OAuth Client ID**. Put the client secret in **OAuth Client
   Secret**.

To remove the client, use `./deploy/revoke-client.sh my-connector` and
reload. See section 6.3.

### 7.5 Limits of the facade

The token endpoint accepts two grants. `authorization_code` does the
steps in the table above. `client_credentials` skips them: a client that
sends its id and secret gets the access token immediately, with no
browser step and no code. A request that gives no `grant_type` is
treated as `client_credentials`, because a connector that sends none
still expects a token. The client secret is the protection in both
cases.

The facade does these checks:

| Check | Behaviour |
| --- | --- |
| Client secret | Compared against a SHA-256 digest. This is the main protection. |
| Authorization code | Random, valid one time, and valid for 10 minutes. |
| Client of the code | The code is only valid for the client that asked for it. |
| Callback address | Must be in the allowlist, and must not change between the two steps. |
| PKCE | If the client sends a `code_challenge`, the token request must show the correct `code_verifier`. Only the S256 method is accepted. |

Keep these limits in mind:

- There is no login. `/oauth/authorize` does not identify a person. It
  shows only that a browser made a request to this server.
- The codes are in a shared memory zone of 4 MB. If the zone becomes
  full, nginx removes the oldest code. A caller that makes many codes
  can thus remove codes that other clients did not use yet. Those
  clients do the authorization again. The rate limit and the length
  limit on the challenge control how quickly this can occur.
- PKCE is verified but not demanded. A client that sends no challenge
  gets a code that needs no verifier. The client secret is still
  necessary.
- All the persons who use one connector get the same access token. The
  facade identifies the connector, not the person.
- The access token does not expire. The `expires_in` value in the token
  response is one year, which causes a client to do the OAuth steps
  again after that time. But the token stays in `tokens.map` and stays
  valid until you revoke it. Revocation is the only expiry.

This is sufficient for one server with one operator. It is not
sufficient if you must know which person made a request.

## 8 Certificate renewal

The `certbot` container makes a check two times each day. It renews the
certificate 30 days before the expiry date.

nginx reloads four times each day to read the new certificate files.
certbot cannot send a signal to a different container. To do this,
certbot must have the Docker socket. This is not a safe procedure, and
thus the reload uses a timer.

### 8.1 To show the certificate status

```sh
docker compose exec certbot certbot certificates
docker compose logs certbot
```

### 8.2 To renew the certificate immediately

1. Start the renewal:

   ```sh
   docker compose run --rm --entrypoint certbot certbot \
     renew --webroot -w /var/www/certbot --force-renewal
   ```

2. Reload nginx:

   ```sh
   docker compose exec nginx nginx -s reload
   ```

## 9 Changes to the limits

The limits are in
`deploy/nginx/templates/mcp.conf.template`.

| Limit | Default | Directive |
| --- | --- | --- |
| Requests for each client | 120 in one minute | `limit_req_zone ... zone=per_client` |
| Requests for each IP address | 300 in one minute | `limit_req_zone ... zone=per_ip` |
| Connections for each IP address | 32 | `limit_conn conn_ip` |
| Requests on port 80 for each IP address | 300 in one minute | `limit_req zone=per_ip` |
| Open streams for each client | 8 | `limit_conn conn_client` |
| Maximum size of a request | 4 MB | `client_max_body_size` |

Port 80 has the same limits and shorter timeouts. It is not a less
important port: certbot renews the certificate through it, thus a flood
that occupies the workers there also stops the renewal.

The connection limit uses the IP address, not the client name. An
unauthenticated request has no client name, and nginx does not count a
limit that has an empty key. A connection flood is unauthenticated.
Thus the IP address is the only key that controls it.

### 9.1 To apply a change

```sh
docker compose up -d --force-recreate nginx
```

> **CAUTION: A reload is not sufficient. The container makes the
> configuration file from the template when it starts. A reload does
> not make the file again.**

## 10 Logs

### 10.1 Request logging

nginx writes no access log by default. The `PRIVACY.md` file of this
project makes that promise for the hosted service, and this stack keeps
the same behaviour.

To turn the log on:

1. Set `ACCESS_LOG=1` in `.env`.
2. Make the container again:

   ```sh
   docker compose up -d --force-recreate nginx
   ```

The log then has one line for each request:

```
"POST /mcp" 200 client=my-laptop auth=bearer diag="-"
```

| Field | Content |
| --- | --- |
| Method and path | The path only. The query string is not included. |
| Status | The HTTP status. |
| `client` | The client name from `tokens.map`. Empty if the request had no valid token. |
| `auth` | The type of the `Authorization` header: `none`, `basic`, `bearer` or `other`. |
| `diag` | The result of an OAuth request: the reason it failed, or `issued/<client id>` when a token was given. `-` for all other requests. |

The log does not contain the `Authorization` header, the query string,
the request body or the IP address.

A response with a 5xx status is logged in this same format even when
`ACCESS_LOG=0`. A stack that hides its own faults cannot be operated: if
the app container stops, each request gives 502, and with no log and the
error log at `crit` there is no message anywhere. The line has the same
fields, thus this costs no more privacy than it must.

> **CAUTION: The `?config=` query string of a Smithery client contains a
> token. This is why no log here contains a query string.**

### 10.2 Error logging

The error log is set to the `crit` level. nginx puts the full request
line, with the query string, in each error message. A lower level thus
writes the tokens of the clients into the log of the operator.

The `crit` level keeps the messages that show a fault of the server:
start failures, certificate faults and worker faults. It removes the
messages for each request.

To see more during an examination, change `error_log /dev/stderr crit;`
to `error_log /dev/stderr error;` in the template and make the container
again. Change it back after the examination.

## 11 Notes

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
  app identify a client without a token. On all other paths nginx
  removes this header, thus a client cannot supply its own name.
- nginx sends the address of the client in the `X-Forwarded-For` header,
  and it writes the header. It does not add to a header from the client.
  The app does not use that address in this configuration: it runs in
  bearer mode, where the rate limit uses the token, and nginx does the
  per-IP control. Thus `ESTNLTK_MCP_TRUSTED_PROXY_HOPS` has no function
  here. The header is correct if you use the app in a different way
  later.
- A CDN or a second proxy in front of nginx needs more than a different
  hop count. nginx writes the header from `$remote_addr`, which is then
  the address of that proxy, thus each visitor of one CDN edge gets the
  same rate limit group and the app never sees the caller. To correct
  this, add the `real_ip` directives to the template with the address
  ranges of that CDN. The template contains an example. Do not use
  `0.0.0.0/0` there: this gives control of the rate limit key to the
  caller.
- nginx finds the address of the app for each request. It does not find
  the address one time when it starts. Thus nginx starts even if the
  app container is down. This is important, because certbot cannot
  renew the certificate if nginx is down.
- No location that nginx can refuse uses `return` inside an `if`. nginx
  answers `return` in the rewrite phase, which is before the preaccess
  phase that contains `limit_req`. A refusal made with `return` is thus
  never counted against the rate limit. The refusals use `limit_except`,
  `auth_request`, `try_files` or njs instead, because all of these
  operate after the preaccess phase. Keep this rule if you add a
  location.
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

## 12 Troubleshooting

### 12.1 The connector gets an error at /oauth/token or /oauth/authorize

The handler puts the reason in the `diag` field of the access log. The
access log is off by default, thus:

1. Set `ACCESS_LOG=1` in `.env`.
2. Make the container again:

   ```sh
   docker compose up -d --force-recreate nginx
   ```

3. Make the connection again, then read the log:

   ```sh
   docker compose logs nginx | grep 'oauth\|/mcp'
   ```

4. Set `ACCESS_LOG=0` again when you are finished.

| `diag` message | Cause | Correction |
| --- | --- | --- |
| `token request carried no client credentials` | The **Advanced settings** fields are empty. | Put the client id and the client secret in the connector. |
| `unknown client: X` | No client with that id. | Make it with `new-oauth-client.sh X`, then reload. |
| `wrong secret for client: X` | The secret does not agree. | Make the client again, then put the new secret in the connector. |
| `no access token is mapped for client: X` | The files do not agree with each other. | Make the client again. This rewrites all three files. |
| `redirect_uri is not in the allowlist: X` | The connector uses a different callback. | Add the address to the `$oauth_redirect_allowed` map in the template, then make the container again. |
| `authorization code is unknown, used or expired` | The code was used one time already, or more than 10 minutes passed. | Do the authorization again in the connector. |
| `code_verifier does not match the code_challenge` | The client sent an incorrect PKCE verifier. | Do the authorization again. If it continues, report it. |
| `redirect_uri must match the authorization request` | The token request gave a different callback, or gave none. | Make sure the connector uses the same callback in both steps. |
| `malformed code_challenge` | The PKCE challenge is not 43 to 128 unreserved characters. | The client does not obey RFC 7636. Report it to the client. |

> **NOTE: Do not use the `auth=` field to find this fault. Claude sends
> the secret in the request body, so `auth=none` is correct for a good
> request. The field shows the type of the `Authorization` header only,
> which is useful for `/mcp`.**

### 12.2 All clients get status 401

nginx starts even if it finds no token file. In this condition, nginx
refuses all clients. This is intentional. If nginx stops, the ACME
challenge on port 80 also stops, and certbot cannot renew the
certificate.

1. Make sure that `deploy/nginx/secrets/tokens.map` exists.
2. Make sure that the file name starts with `tokens` and ends with
   `.map`. nginx reads only these files.
3. Reload nginx.

## 13 Reference

- Server code and the internal authentication: [`server.py`](../server.py)
- Compose configuration: [`docker-compose.yaml`](../docker-compose.yaml)
- nginx configuration: [`mcp.conf.template`](nginx/templates/mcp.conf.template)
- Token endpoint handler: [`njs/oauth.js`](nginx/njs/oauth.js)
- Values rendered into the template: [`render-vars.envsh`](nginx/render-vars.envsh)

Scripts:

| Script | Function |
| --- | --- |
| [`init-letsencrypt.sh`](init-letsencrypt.sh) | Gets the first Let's Encrypt certificate. |
| [`local-cert.sh`](local-cert.sh) | Makes a self-signed certificate for local tests. |
| [`new-token.sh`](new-token.sh) | Makes a token for a client that sends a header. |
| [`new-oauth-client.sh`](new-oauth-client.sh) | Makes the credentials for an OAuth client. |
| [`revoke-client.sh`](revoke-client.sh) | Removes all the credentials of one client. |
| [`lib.sh`](lib.sh) | Shared functions. The scripts source this file. |
