# Stdlib-only fixed Worker. Build/pull requires approval of the deployment review.
# BASE_IMAGE must be the platform digest in runtime.lock.json, never a mutable tag.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY --chown=0:0 ii_pro_worker/runtime.py ii_pro_worker/wire.py ii_pro_worker/probes.py ii_pro_worker/school_contract.py ii_pro_worker/python_runner.py ii_pro_worker/app_contract.py ii_pro_worker/app_runner.py /opt/worker/
WORKDIR /work
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/python", "-I", "-S", "-B", "/opt/worker/runtime.py"]
