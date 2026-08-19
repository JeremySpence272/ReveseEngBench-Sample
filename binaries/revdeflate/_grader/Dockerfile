FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 coreutils ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /grader
# Vendored stdlib byte-comparison grader (grade.py) + the counting wrapper.
COPY src/ /grader/src/
COPY score.py /grader/score.py
COPY test.sh /grader/test.sh
# Private ground truth (originals.tar + manifest.json + keys.txt). Never shipped
# to the agent; baked here only so the grader can byte-compare the submission.
# originals/ is shipped as a Linux-created tar and extracted here so file modes
# and raw (non-normalized) filename bytes are reconstituted faithfully inside the
# image, regardless of the host filesystem the repo lives on.
COPY challenges/ /grader/challenges/
# -p preserves the exact stored file modes: the strict grader compares permission
# bits, so the baked ground truth must keep the faithful perms the challenge was
# generated with. Do NOT chmod the originals afterwards. There is no unprivileged
# user in this grader (the submission is inert data compared by root -- nothing
# from it is ever executed), so the answer key needs no hiding.
RUN cd /grader/challenges && tar -pxf originals.tar && rm -f originals.tar
RUN chmod +x /grader/test.sh /grader/score.py
CMD ["/grader/test.sh"]
