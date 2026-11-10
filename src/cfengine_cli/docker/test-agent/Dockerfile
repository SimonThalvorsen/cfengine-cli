FROM debian:13-slim
ENV PATH="/root/.local/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 pipx sudo procps git rsync

RUN pipx install cfengine
RUN pipx install cfbs

RUN cfengine install --clients localhost --edition community

RUN mkdir -p /var/cfengine/bin/ \
    && ln -sf "$(command -v python3)" /var/cfengine/bin/cfengine-selected-python

RUN mkdir -p /var/cfengine/modules/promises /tmp/assert-module-fetch \
    && cd /tmp/assert-module-fetch \
    && cfbs --non-interactive --masterfiles no --git no init \
    && cfbs --non-interactive add library-for-promise-types-in-python promise-type-assert \
    && cfbs build \
    && cp out/masterfiles/modules/promises/assert_promise_type.py \
        out/masterfiles/modules/promises/cfengine_module_library.py \
        /var/cfengine/modules/promises/ \
    && cd / && rm -rf /tmp/assert-module-fetch

COPY assert-module/promise_agent.cf /var/cfengine/modules/promises/promise_agent.cf
