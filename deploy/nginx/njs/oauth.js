/*
 * OAuth endpoints for the estonian-mcp facade.
 *
 * This exists because nginx core cannot read a request body, and
 * Claude's connector sends its client secret as a form field
 * (client_secret_post) rather than in an Authorization header. auth_basic
 * never sees that secret, so it can never authenticate that client.
 *
 * Everything else about the facade stays in nginx config. This file
 * does five things:
 *
 *   1. mint a single-use authorization code at /oauth/authorize and
 *      remember the PKCE challenge, the client id and the callback that
 *      came with it, in the shared dictionary declared by
 *      js_shared_dict_zone;
 *   2. pull client_id and client_secret out of the token request, from
 *      either the body (client_secret_post) or a Basic header
 *      (client_secret_basic), so both kinds of client work;
 *   3. compare the presented secret against a SHA-256 digest held in
 *      secrets/oauth_secrets.map;
 *   4. redeem the code: once, before it expires, for the client it was
 *      issued to, against the PKCE verifier if one was promised;
 *   5. look up that client's access token in secrets/oauth_tokens.map
 *      and answer with a token response.
 *
 * The two map lookups are ordinary nginx `map` blocks keyed on
 * $oauth_client_id, which this script sets. So credentials still live in
 * the same map files as everything else, and adding a client is still a
 * reload.
 *
 * What this facade is NOT: it has no users and no sessions. Nobody logs
 * in at /oauth/authorize, so the code proves only that a browser reached
 * this server. The client secret is what authenticates. See section 7.5
 * of deploy/README.md.
 */

var hashes = require('crypto');

var TOKEN_LIFETIME_SECONDS = 31536000; // a year; there is no refresh endpoint

// Must match the timeout= on js_shared_dict_zone in the template. It is
// repeated here only to be reported in error_description.
var CODE_TTL_SECONDS = 600;

/*
 * Everything written to a log goes through here first.
 *
 * nginx does not escape what it writes to a log, and every value below
 * arrives percent-decoded from an attacker. Without this, a client id of
 * "x\n2026-01-01 00:00:00 [error] ..." forges a log line, and an
 * unbounded redirect_uri fills a disk one request at a time.
 */
function safe(value) {
    var s = String(value === undefined || value === null ? '' : value);
    var out = '';
    for (var i = 0; i < s.length && out.length < 100; i++) {
        var c = s.charCodeAt(i);
        // Printable ASCII only, minus the quote and backslash that would
        // let a value break out of the field it is logged in.
        if (c >= 0x20 && c < 0x7f && c !== 0x22 && c !== 0x5c) {
            out += s[i];
        } else {
            out += '.';
        }
    }
    if (s.length > out.length) {
        out += '...';
    }
    return out;
}

/*
 * One reason, one place. $oauth_diag is a js_var that the access log
 * reads, which is where OAuth failures are visible: the error log is
 * held at `crit` because nginx stamps every line there with the full
 * request line, query string included.
 */
function diag(r, message) {
    var text = safe(message);
    r.variables.oauth_diag = text;
    // Also to the error log, for an operator who has deliberately turned
    // the level back up. Sanitised either way.
    r.error('oauth: ' + text);
}

/*
 * The authorization endpoint. A browser lands here, and it redirects to
 * the client's callback carrying a fresh single-use code.
 *
 * This is a content-phase handler rather than an nginx `return` on
 * purpose. `return` is answered in the rewrite phase, which precedes
 * the preaccess phase where limit_req lives, so a config-level redirect
 * here would be unmetered in both its success and its failure branch.
 *
 * It also lets the redirect_uri allowlist hold plain URLs: njs decodes
 * the incoming value, and re-encodes state when rebuilding the callback.
 */
/*
 * njs hands back an ARRAY when a query parameter appears more than once,
 * and a string otherwise. Every read below wants one value, and calling
 * a string method on the array form throws inside the handler, which
 * answers 500 with nginx's HTML error page and, under the shipped log
 * settings, no explanation anywhere.
 */
function arg(r, name) {
    var value = r.args[name];
    if (value === undefined || value === null) {
        return '';
    }
    if (Array.isArray(value)) {
        return value.length ? String(value[0]) : '';
    }
    return String(value);
}

function authorize(r) {
    var redirectUri = arg(r, 'redirect_uri');

    // Assign before reading the map, which resolves lazily on first read.
    r.variables.oauth_redirect_uri = redirectUri;

    if (r.variables.oauth_redirect_allowed !== '1') {
        diag(r, 'redirect_uri is not in the allowlist: ' + redirectUri);
        badRequest(r, 'invalid_request', 'redirect_uri is not in the allowlist');
        return;
    }

    // An unknown client gets no code at all. The code is worthless
    // without the secret, but minting one for an id that does not exist
    // only ever hides a typo in the connector's settings.
    var clientId = arg(r, 'client_id');
    if (!clientId) {
        diag(r, 'authorize request carried no client_id');
        badRequest(r, 'invalid_request', 'client_id is required');
        return;
    }
    r.variables.oauth_client_id = clientId;
    if (!r.variables.oauth_client_secret) {
        diag(r, 'authorize request for unknown client: ' + clientId);
        badRequest(r, 'invalid_client', 'unknown client_id');
        return;
    }

    var challenge = arg(r, 'code_challenge');
    var method = (arg(r, 'code_challenge_method') || 'plain').toUpperCase();
    if (challenge && method !== 'S256') {
        // Only S256 is advertised, and `plain` is no binding at all: the
        // verifier equals the challenge, so anyone holding the code holds
        // the verifier too.
        diag(r, 'unsupported code_challenge_method: ' + method);
        badRequest(r, 'invalid_request', 'only the S256 code_challenge_method is supported');
        return;
    }
    // An S256 challenge is a base64url SHA-256 digest: 43 characters, no
    // padding. Anything else can never redeem, and storing it verbatim
    // would let an unauthenticated caller push kilobytes into the shared
    // zone on every request and evict codes other people are still
    // waiting to redeem. RFC 7636 allows 43 to 128 characters, so the
    // upper bound is its bound rather than ours.
    if (challenge && !/^[A-Za-z0-9\-._~]{43,128}$/.test(challenge)) {
        diag(r, 'malformed code_challenge');
        badRequest(r, 'invalid_request',
                   'code_challenge must be 43 to 128 unreserved characters');
        return;
    }

    var code = newCode(r);
    var stored = JSON.stringify({
        i: clientId,
        u: redirectUri,
        c: challenge
    });

    try {
        ngx.shared.oauth_codes.set(code, stored);
    } catch (e) {
        diag(r, 'could not store the authorization code: ' + e.message);
        r.headersOut['Content-Type'] = 'application/json';
        r.headersOut['Cache-Control'] = 'no-store';
        r.return(500, JSON.stringify({
            error: 'server_error',
            error_description: 'the authorization code could not be stored'
        }));
        return;
    }

    var target = redirectUri
        + (redirectUri.indexOf('?') < 0 ? '?' : '&')
        + 'code=' + encodeURIComponent(code);

    var state = arg(r, 'state');
    if (state !== '') {
        target += '&state=' + encodeURIComponent(state);
    }

    // For a 3xx, njs uses the second argument as the Location header.
    // Assigning r.headersOut.Location instead leaves it empty.
    r.return(302, target);
}

function token(r) {
    if (r.method !== 'POST') {
        // No CORS preflight branch: the token endpoint sends no
        // Access-Control-Allow-Origin, so a browser has no reason to
        // preflight it and no way to use the answer.
        fail(r, 405, 'invalid_request', 'the token endpoint accepts POST', false);
        return;
    }

    var form = parseForm(r.requestText || '');
    var presented = credentials(r, form);

    if (!presented.id) {
        diag(r, 'token request carried no client credentials');
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
        diag(r, 'unknown client: ' + presented.id);
        fail(r, 401, 'invalid_client', 'client authentication failed',
             presented.fromHeader);
        return;
    }

    var presentedDigest = sha256Hex(presented.secret);
    if (!constantTimeEquals(expectedDigest, presentedDigest)) {
        diag(r, 'wrong secret for client: ' + presented.id);
        fail(r, 401, 'invalid_client', 'client authentication failed',
             presented.fromHeader);
        return;
    }

    // The client is authenticated from here on, which is why the code is
    // only redeemed now: redeeming it first would let an anonymous caller
    // burn another client's code by guessing at it.
    var grant = form['grant_type'] || 'client_credentials';
    if (grant === 'authorization_code') {
        if (!redeem(r, presented.id, form)) {
            return;
        }
    } else if (grant !== 'client_credentials') {
        diag(r, 'unsupported grant_type: ' + grant);
        badRequest(r, 'unsupported_grant_type',
                   'supported grant types are authorization_code and client_credentials');
        return;
    }

    var accessToken = r.variables.oauth_access_token || '';
    if (!accessToken) {
        // Authenticated, but nothing to hand back. Fail loudly here
        // rather than issuing an empty token that would fail later at
        // /mcp with a 401 pointing nowhere near the cause.
        diag(r, 'no access token is mapped for client: ' + presented.id);
        fail(r, 500, 'server_error', 'no access token is mapped for this client', false);
        return;
    }

    r.variables.oauth_diag = 'issued/' + safe(presented.id);

    r.headersOut['Content-Type'] = 'application/json';
    // RFC 6749 requires no-store on token responses.
    r.headersOut['Cache-Control'] = 'no-store';
    r.headersOut['Pragma'] = 'no-cache';
    r.return(200, JSON.stringify({
        access_token: accessToken,
        token_type: 'Bearer',
        expires_in: TOKEN_LIFETIME_SECONDS,
        scope: 'mcp'
    }));
}

/*
 * Redeems an authorization code. Returns true when the caller may
 * continue; on false it has already answered the request.
 *
 * Reading and deleting are two calls rather than one pop(), because
 * pop() answers undefined on a zone that has a timeout in njs 1.0.0
 * even when get() finds the entry - which would refuse every code.
 * Single use is still atomic: delete() reports whether THIS request was
 * the one that removed the key, so of two simultaneous redemptions
 * exactly one continues, in whichever worker process it lands.
 */
function redeem(r, clientId, form) {
    var code = form['code'] || '';
    if (!code) {
        diag(r, 'authorization_code grant without a code');
        badRequest(r, 'invalid_request', 'code is required for the authorization_code grant');
        return false;
    }

    var raw;
    var claimed = false;
    try {
        raw = ngx.shared.oauth_codes.get(code);
        if (raw !== undefined) {
            claimed = ngx.shared.oauth_codes.delete(code);
        }
    } catch (e) {
        raw = undefined;
    }
    if (raw === undefined || !claimed) {
        // Unknown, already redeemed, or older than CODE_TTL_SECONDS. The
        // three are deliberately one answer: distinguishing them tells an
        // attacker which codes once existed.
        diag(r, 'authorization code is unknown, used or expired');
        badRequest(r, 'invalid_grant',
                   'the authorization code is unknown, already used, or older than '
                   + CODE_TTL_SECONDS + ' seconds');
        return false;
    }

    var entry;
    try {
        entry = JSON.parse(raw);
    } catch (e) {
        entry = {};
    }

    if (entry.i && entry.i !== clientId) {
        diag(r, 'authorization code belongs to another client: ' + clientId);
        badRequest(r, 'invalid_grant', 'the authorization code was issued to another client');
        return false;
    }

    // RFC 6749 section 4.1.3: when the authorization request carried a
    // redirect_uri, the token request must present the same one. Every
    // code issued here carries one, because /oauth/authorize refuses a
    // callback that is not in the allowlist - so accepting a redemption
    // that simply omits the field would make the binding optional at the
    // attacker's choice, which is no binding at all.
    var presentedRedirect = form['redirect_uri'] || '';
    if (entry.u && entry.u !== presentedRedirect) {
        diag(r, 'redirect_uri does not match the authorization request');
        badRequest(r, 'invalid_grant', 'redirect_uri must match the authorization request');
        return false;
    }

    if (entry.c) {
        var verifier = form['code_verifier'] || '';
        if (!verifier) {
            diag(r, 'code_verifier missing for a code bound to a challenge');
            badRequest(r, 'invalid_grant', 'code_verifier is required for this code');
            return false;
        }
        if (!constantTimeEquals(entry.c, sha256Base64Url(verifier))) {
            diag(r, 'code_verifier does not match the code_challenge');
            badRequest(r, 'invalid_grant', 'code_verifier does not match the code_challenge');
            return false;
        }
    }

    return true;
}

/*
 * RFC 6749 defines two ways for a client to authenticate here. Claude
 * uses the second one regardless of what the server advertises in
 * token_endpoint_auth_methods_supported, so both are accepted.
 */
function credentials(r, form) {
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

/*
 * 16 cryptographically random bytes, hex encoded.
 *
 * Deliberately NOT $request_id, which nginx builds from random() seeded
 * with the worker pid and the start time. Every caller of
 * /oauth/authorize is handed one of those values, so the stream is
 * directly observable by anyone who asks, and that generator is
 * reconstructible from a few dozen samples. A code is only half a
 * credential, but it is not the half worth economising on.
 */
function newCode(r) {
    try {
        var bytes = new Uint8Array(16);
        crypto.getRandomValues(bytes);
        var hex = '';
        for (var i = 0; i < bytes.length; i++) {
            hex += (bytes[i] < 16 ? '0' : '') + bytes[i].toString(16);
        }
        return hex;
    } catch (e) {
        // An njs built without WebCrypto. Weaker, and still 16 bytes.
        return r.variables.request_id;
    }
}

function sha256Hex(s) {
    return hashes.createHash('sha256').update(s).digest('hex');
}

/*
 * PKCE S256: base64url(SHA-256(verifier)), unpadded, per RFC 7636
 * appendix A. The conversion is done by hand rather than with a
 * 'base64url' digest encoding, which older njs builds do not have.
 */
function sha256Base64Url(s) {
    return hashes.createHash('sha256').update(s).digest('base64')
        .replace(/\+/g, '-')
        .replace(/\//g, '_')
        .replace(/=+$/, '');
}

/*
 * Compares two strings without an early return on the first differing
 * byte. The length check can leak the length, which for a fixed-width
 * digest is not a secret.
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

function badRequest(r, code, description) {
    fail(r, 400, code, description, false);
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
