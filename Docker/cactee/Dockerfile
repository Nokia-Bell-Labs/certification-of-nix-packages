# © 2026 Nokia
# Licensed under the MIT License
# SPDX-License-Identifier: MIT

FROM python:3.12-slim-bookworm

COPY cactee/ca/ /usr/local/share/ca-certificates/
RUN if ls /usr/local/share/ca-certificates/*.crt >/dev/null 2>&1; then \
        cat /etc/ssl/certs/ca-certificates.crt \
            /usr/local/share/ca-certificates/*.crt \
            > /etc/ssl/certs/ca-with-zscaler.pem; \
    else \
        cp /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-with-zscaler.pem; \
    fi
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-with-zscaler.pem \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-with-zscaler.pem \
    PIP_CERT=/etc/ssl/certs/ca-with-zscaler.pem \
    CACTEE_ROOT=/cactee
RUN pip install --no-cache-dir huggingface_hub docker requests pyyaml cryptography \
    || pip install --no-cache-dir \
         --trusted-host pypi.org \
         --trusted-host files.pythonhosted.org \
         huggingface_hub docker requests pyyaml cryptography

COPY cactee/cactee-server.py /usr/local/bin/cactee-server.py
EXPOSE 8080
CMD ["python3", "/usr/local/bin/cactee-server.py"]
