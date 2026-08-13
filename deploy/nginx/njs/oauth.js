/*
 * OAuth token endpoint for the estonian-mcp facade.
 *
 * This exists because nginx core cannot read a request body, and
 * Claude's connector sends its client secret as a form field
 * (client_secret_post) rather than in an Authorization header. auth_basic
 * never sees that secret, so it can never authenticate that client.
 *
 * Everything else about the facade stays in nginx config. This file
 * does exactly four things:
 *
 *   1. pull client_id and client_secret out of the request, from either
 *      the body (client_secret_post) or a Basic header
 *      (client_secret_basic), so both kinds of client work;
 *   2. compare the presented secret against a SHA-256 digest held in
 *      secrets/oauth_secrets.map;
 *   3. look up that client's access token in secrets/oauth_tokens.map;
 *   4. answer with a token response.
 *
 * Both lookups are ordinary nginx `map` blocks keyed on $oauth_client_id,
 * which this script sets. So credentials still live in the same map
 * files as everything else, and adding a client is still a reload.
 *
 * Nothing here verifies the authorization code or PKCE. The client
 * secret is the whole of the security. See section 7.4 of the README.
 */

var crypto = require('crypto');

var TOKEN_LIFETIME_SECONDS = 31536000; // a year; there is no refresh endpoint

/*
 * The authorization endpoint. A browser lands here, and it redirects to
 * the client's callback carrying a fixed code.
 *
 * This is a content-phase handler rather than an nginx `return` on
 * purpose. `return` is answered in the rewrite phase, which precedes
 * the preaccess phase where limit_req lives, so a config-level redirect
 * here would be unmetered in both its success and its failure branch.
 *
 * It also lets the redirect_uri allowlist hold plain URLs: njs decodes
 * the incoming value, and re-encodes state when rebuilding the callback.
 */
function authorize(r) {
    var redirectUri = r.args.redirect_uri || '';

    // Assign before reading the map, which resolves lazily on first read.
    r.variables.oauth_redirect_uri = redirectUri;

    if (r.variables.oauth_redirect_allowed !== '1') {
        r.error('oauth: redirect_uri is not in the allowlist: "' + redirectUri + '"');
        r.headersOut['Content-Type'] = 'application/json';
        r.return(400, JSON.stringify({
            error: 'invalid_request',
            error_description: 'redirect_uri is not in the allowlist'
        }));
        return;
    }

    var target = redirectUri
        + (redirectUri.indexOf('?') < 0 ? '?' : '&')
        + 'code=' + encodeURIComponent(r.variables.oauth_code || '');

    var state = r.args.state;
    if (state !== undefined && state !== '') {
        target += '&state=' + encodeURIComponent(state);
    }

    // For a 3xx, njs uses the second argument as the Location header.
    // Assigning r.headersOut.Location instead leaves it empty.
    r.return(302, target);
}

function token(r) {
    // Preflight never carries credentials, so answer it before anything
    // else looks for them.
    if (r.method === 'OPTIONS') {
        r.headersOut['Access-Control-Allow-Origin'] = '*';
        r.headersOut['Access-Control-Allow-Headers'] = 'authorization,content-type';
        r.headersOut['Access-Control-Allow-Methods'] = 'POST,OPTIONS';
        r.return(204);
        return;
    }

    if (r.method !== 'POST') {
        fail(r, 405, 'invalid_request', 'the token endpoint accepts POST', false);
        return;
    }

    var presented = credentials(r);

    if (!presented.id) {
        r.error('oauth: token request carried no client credentials');
        fail(r, 401, 'invalid_client', 'no client credentials were supplied',
             presented.fromHeader);
        return;
    }

    // Setting this makes the two map lookups below resolve. The maps are
    // evaluated lazily on first read, so the order matters: assign, then
    // read.
    r.variables.oauth_client_id = presented.id;

    var expectedDigest = r.variables.oauth_client_secret || '';
    if (!expectedDigest) {
        r.error('oauth: unknown client "' + presented.id + '"');
        fail(r, 401, 'invalid_client', 'client authentication failed',
             presented.fromHeader);
        return;
    }

    var presentedDigest = sha256Hex(presented.secret);
    if (!constantTimeEquals(expectedDigest, presentedDigest)) {
        r.error('oauth: wrong secret for client "' + presented.id + '"');
        fail(r, 401, 'invalid_client', 'client authentication failed',
             presented.fromHeader);
        return;
    }

    var accessToken = r.variables.oauth_access_token || '';
    if (!accessToken) {
        // Authenticated, but nothing to hand back. Fail loudly here
        // rather than issuing an empty token that would fail later at
        // /mcp with a 401 pointing nowhere near the cause.
        r.error('oauth: no access token is mapped for client "' + presented.id + '"');
        fail(r, 500, 'server_error', 'no access token is mapped for this client', false);
        return;
    }

    r.log('oauth: issued a token to client "' + presented.id + '"');

    r.headersOut['Content-Type'] = 'application/json';
    // RFC 6749 requires no-store on token responses.
    r.headersOut['Cache-Control'] = 'no-store';
    r.headersOut['Pragma'] = 'no-cache';
    r.headersOut['Access-Control-Allow-Origin'] = '*';
    r.return(200, JSON.stringify({
        access_token: accessToken,
        token_type: 'Bearer',
        expires_in: TOKEN_LIFETIME_SECONDS,
        scope: 'mcp'
    }));
}

/*
 * RFC 6749 defines two ways for a client to authenticate here. Claude
 * uses the second one regardless of what the server advertises in
 * token_endpoint_auth_methods_supported, so both are accepted.
 */
function credentials(r) {
    var auth = r.headersIn['Authorization'] || '';

    if (auth.slice(0, 6).toLowerCase() === 'basic ') {
        var decoded = '';
        try {
            decoded = Buffer.from(auth.slice(6).trim(), 'base64').toString('utf8');
        } catch (e) {
            decoded = '';
        }
        var colon = decoded.indexOf(':');
        if (colon > 0) {
            // RFC 6749 form-urlencodes both halves before base64.
            return {
                id: formDecode(decoded.slice(0, colon)),
                secret: formDecode(decoded.slice(colon + 1)),
                fromHeader: true
            };
        }
    }

    // client_secret_post. r.requestText is populated because js_content
    // reads the body first; the location caps and buffers it so it stays
    // in memory rather than spilling to a temp file.
    var form = parseForm(r.requestText || '');
    return {
        id: form['client_id'] || '',
        secret: form['client_secret'] || '',
        fromHeader: false
    };
}

function parseForm(text) {
    var out = {};
    var parts = text.split('&');
    for (var i = 0; i < parts.length; i++) {
        var eq = parts[i].indexOf('=');
        if (eq < 1) {
            continue;
        }
        out[formDecode(parts[i].slice(0, eq))] = formDecode(parts[i].slice(eq + 1));
    }
    return out;
}

function formDecode(s) {
    try {
        return decodeURIComponent(s.replace(/\+/g, ' '));
    } catch (e) {
        return '';
    }
}

function sha256Hex(s) {
    return crypto.createHash('sha256').update(s).digest('hex');
}

/*
 * Compares two hex digests without an early return on the first
 * differing byte. The length check can leak the length, which for a
 * fixed-width digest is not a secret.
 */
function constantTimeEquals(a, b) {
    if (a.length !== b.length) {
        return false;
    }
    var diff = 0;
    for (var i = 0; i < a.length; i++) {
        diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
    }
    return diff === 0;
}

function fail(r, status, code, description, challenge) {
    r.headersOut['Content-Type'] = 'application/json';
    r.headersOut['Cache-Control'] = 'no-store';
    // Only challenge a client that actually tried the Authorization
    // header. Answering a body-authenticated client with a Basic
    // challenge would invite it to retry a method it does not use.
    if (challenge) {
        r.headersOut['WWW-Authenticate'] = 'Basic realm="estonian-mcp oauth client"';
    }
    r.return(status, JSON.stringify({
        error: code,
        error_description: description
    }));
}

export default { authorize, token };
