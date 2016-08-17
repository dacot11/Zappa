#!/usr/bin/env python
"""
Create and install a Let's Encrypt cert for an API Gateway.

This file is a descendant of @diafygi's 'acme-tiny',
with http-01 replaced with dns-01 via AWS Route 53.

You must generate your own account.key:
openssl genrsa 2048 > account.key # Keep it secret, keep safe!

"""

import argparse, subprocess, json, os, sys, base64, binascii, time, hashlib, re, copy, textwrap, logging
try:
    from urllib.request import urlopen # Python 3
except ImportError:
    from urllib2 import urlopen # Python 2

# Staging
# Amazon doesn't accept these though.
# DEFAULT_CA = "https://acme-staging.api.letsencrypt.org"

# Production
DEFAULT_CA = "https://acme-v01.api.letsencrypt.org"

LOGGER = logging.getLogger(__name__)
LOGGER.addHandler(logging.StreamHandler())

def get_cert_and_update_domain(zappa_instance, lambda_name, api_stage, domain):
    """
    Main cert installer path.
    TODO: Replace z_i with local instance, since we won't have a Zappa on the server..
    """

    try:
        create_domain_key()
        create_domain_csr(domain)
        get_cert(zappa_instance)
        created_chained_certificate()

        with open('/tmp/signed.crt') as f:
            certificate_body = f.read()

        with open('/tmp/domain.key') as f:
            certificate_private_key = f.read()

        with open('/tmp/intermediate.pem') as f:
            certificate_chain = f.read()

        if not zappa_instance.get_domain_name(domain):

            zappa_instance.create_domain_name(
                domain, 
                domain + "-Zappa-LE-Cert", 
                certificate_body, 
                certificate_private_key,
                certificate_chain,
                lambda_name,
                api_stage
                )
            print("Created a new domain name. Please not that it can take up to 40 minutes for this domain to be created and propagated through AWS, but it requires no further work on your part.")
        else:
            zappa_instance.update_domain_name(
                domain, 
                domain + "-Zappa-LE-Cert", 
                certificate_body, 
                certificate_private_key,
                certificate_chain 
                )

    except Exception as e:
        print(e)
        pass

    # Always clean-up.
    cleanup()
    return True

def create_domain_key():
    """
    """
    proc = subprocess.Popen(["openssl genrsa 2048 > /tmp/domain.key"], 
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    out, err = proc.communicate()
    if proc.returncode != 0:
        raise IOError("OpenSSL Error: {0}".format(err))
    return True

def create_domain_csr(domain):
    """
    """
    subj = "/CN=" + domain
    cmd = 'openssl req -new -sha256 -key /tmp/domain.key -subj "{}"  > /tmp/domain.csr'.format(subj)
    proc = subprocess.Popen([cmd],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    out, err = proc.communicate()
    if proc.returncode != 0:
        raise IOError("OpenSSL Error: {0}".format(err))
    return True

def created_chained_certificate():
    """
    """
    proc = subprocess.Popen(["wget -O - https://letsencrypt.org/certs/lets-encrypt-x3-cross-signed.pem > /tmp/intermediate.pem"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    out, err = proc.communicate()
    if proc.returncode != 0:
        raise IOError("Error: {0}".format(err))

    proc = subprocess.Popen(["cat /tmp/signed.crt /tmp/intermediate.pem > /tmp/chained.pem"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    out, err = proc.communicate()
    if proc.returncode != 0:
        raise IOError("Error: {0}".format(err))

    return True

def get_cert(zappa_instance, log=LOGGER, CA=DEFAULT_CA):
    """

    """
    
    def _b64(b):
        """
        Helper function base64 encode for jose spec
        """
        return base64.urlsafe_b64encode(b).decode('utf8').replace("=", "")

    # parse account key to get public key
    log.info("Parsing account key...")
    proc = subprocess.Popen(["openssl rsa -in /tmp/account.key -noout -text"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    out, err = proc.communicate()
    if proc.returncode != 0:
        raise IOError("OpenSSL Error: {0}".format(err))
    pub_hex, pub_exp = re.search(
        r"modulus:\n\s+00:([a-f0-9\:\s]+?)\npublicExponent: ([0-9]+)",
        out.decode('utf8'), re.MULTILINE|re.DOTALL).groups()
    pub_exp = "{0:x}".format(int(pub_exp))
    pub_exp = "0{0}".format(pub_exp) if len(pub_exp) % 2 else pub_exp
    header = {
        "alg": "RS256",
        "jwk": {
            "e": _b64(binascii.unhexlify(pub_exp.encode("utf-8"))),
            "kty": "RSA",
            "n": _b64(binascii.unhexlify(re.sub(r"(\s|:)", "", pub_hex).encode("utf-8"))),
        },
    }
    accountkey_json = json.dumps(header['jwk'], sort_keys=True, separators=(',', ':'))
    thumbprint = _b64(hashlib.sha256(accountkey_json.encode('utf8')).digest())

    def _send_signed_request(url, payload):
        """
        Helper function to make signed requests to Boulder
        """
        payload64 = _b64(json.dumps(payload).encode('utf8'))
        protected = copy.deepcopy(header)
        protected["nonce"] = urlopen(CA + "/directory").headers['Replay-Nonce']
        protected64 = _b64(json.dumps(protected).encode('utf8'))
        proc = subprocess.Popen(["openssl dgst -sha256 -sign /tmp/account.key"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
        out, err = proc.communicate("{0}.{1}".format(protected64, payload64).encode('utf8'))
        if proc.returncode != 0:
            raise IOError("OpenSSL Error: {0}".format(err))
        data = json.dumps({
            "header": header, "protected": protected64,
            "payload": payload64, "signature": _b64(out),
        })
        try:
            resp = urlopen(url, data.encode('utf8'))
            return resp.getcode(), resp.read()
        except IOError as e:
            return getattr(e, "code", None), getattr(e, "read", e.__str__)()

    # find domains
    log.info("Parsing CSR...")
    proc = subprocess.Popen(["openssl req -in /tmp/domain.csr -noout -text"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    out, err = proc.communicate()
    if proc.returncode != 0:
        raise IOError("Error loading {0}: {1}".format(csr, err))
    domains = set([])
    common_name = re.search(r"Subject:.*? CN=([^\s,;/]+)", out.decode('utf8'))
    if common_name is not None:
        domains.add(common_name.group(1))
    subject_alt_names = re.search(r"X509v3 Subject Alternative Name: \n +([^\n]+)\n", out.decode('utf8'), re.MULTILINE|re.DOTALL)
    if subject_alt_names is not None:
        for san in subject_alt_names.group(1).split(", "):
            if san.startswith("DNS:"):
                domains.add(san[4:])

    # get the certificate domains and expiration
    log.info("Registering account...")
    code, result = _send_signed_request(CA + "/acme/new-reg", {
        "resource": "new-reg",
        "agreement": "https://letsencrypt.org/documents/LE-SA-v1.1.1-August-1-2016.pdf",
    })
    if code == 201:
        log.info("Registered!")
    elif code == 409:
        log.info("Already registered!")
    else:
        raise ValueError("Error registering: {0} {1}".format(code, result))

    # verify each domain
    for domain in domains:
        log.info("Verifying {0}...".format(domain))

        # get new challenge
        code, result = _send_signed_request(CA + "/acme/new-authz", {
            "resource": "new-authz",
            "identifier": {"type": "dns", "value": domain},
        })
        if code != 201:
            raise ValueError("Error requesting challenges: {0} {1}".format(code, result))

        challenge = [ch for ch in json.loads(result.decode('utf8'))['challenges'] if ch['type'] == "dns-01"][0]
        token = re.sub(r"[^A-Za-z0-9_\-]", "_", challenge['token'])
        keyauthorization = "{0}.{1}".format(token, thumbprint)

        # sha256_b64
        digest = _b64(hashlib.sha256(keyauthorization).digest())

        zone_id = zappa_instance.get_hosted_zone_id_for_domain(domain)
        if not zone_id:
            raise ValueError("Could not find Zone ID for: " + domain)
        resp = zappa_instance.set_dns_challenge_txt(zone_id, domain, digest)

        print("Waiting for DNS to propagate..")

        # What's optimal here?
        import time
        time.sleep(45)

        # notify challenge are met
        code, result = _send_signed_request(challenge['uri'], {
            "resource": "challenge",
            "keyAuthorization": keyauthorization,
        })
        if code != 202:
            raise ValueError("Error triggering challenge: {0} {1}".format(code, result))

        # wait for challenge to be verified
        while True:
            try:
                resp = urlopen(challenge['uri'])
                challenge_status = json.loads(resp.read().decode('utf8'))
            except IOError as e:
                raise ValueError("Error checking challenge: {0} {1}".format(
                    e.code, json.loads(e.read().decode('utf8'))))
            if challenge_status['status'] == "pending":
                time.sleep(2)
            elif challenge_status['status'] == "valid":
                log.info("{0} verified!".format(domain))
                break
            else:
                raise ValueError("{0} challenge did not pass: {1}".format(
                    domain, challenge_status))

    # get the new certificate
    log.info("Signing certificate...")
    proc = subprocess.Popen(["openssl req -in /tmp/domain.csr -outform DER"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
    csr_der, err = proc.communicate()
    code, result = _send_signed_request(CA + "/acme/new-cert", {
        "resource": "new-cert",
        "csr": _b64(csr_der),
    })
    if code != 201:
        raise ValueError("Error signing certificate: {0} {1}".format(code, result))

    # return signed certificate!
    log.info("Certificate signed!")
    cert_body = """-----BEGIN CERTIFICATE-----\n{0}\n-----END CERTIFICATE-----\n""".format(
"\n".join(textwrap.wrap(base64.b64encode(result).decode('utf8'), 64)))

    signed_crt = open("/tmp/signed.crt", "w")
    signed_crt.write(cert_body)
    signed_crt.close()

    return True

def cleanup():
    """
    Delete any temporary files.
    """

    filenames = [
        '/tmp/account.key',
        '/tmp/domain.key',
        '/tmp/domain.csr',
        '/tmp/signed.crt',
        '/tmp/intermediate.pem',
        '/tmp/chained.pem',
        '/tmp/lets-encrypt-x3-cross-signed.pem'
    ]

    for filename in filenames:
        try:
            os.remove(filename)
        except OSError:
            pass

    return True