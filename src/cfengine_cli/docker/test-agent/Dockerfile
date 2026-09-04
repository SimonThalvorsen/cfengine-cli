# Install cfengine community (client)
# `cfengine test` never needs a real hub, given that it is only supposed
# to check and execute a policy set.
FROM debian:12-slim
ENV PATH="/root/.local/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 pipx sudo
RUN pipx install cfengine --force
# RUN cfengine install --hub localhost --edition community
# Does not work since localhost resolves to `local` in cfengine cli
RUN cfengine install --clients localhost --edition community

# The package promise module wrapper (and other Python-backed modules) look
# for an interpreter here
RUN ln -s "$(command -v python3)" /var/cfengine/bin/cfengine-selected-python
