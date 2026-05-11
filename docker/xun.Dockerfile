# A reference Dockerfile for xun-box
FROM xun-base:latest
COPY . /xun-src
WORKDIR /xun-src
RUN pip install .

RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/
RUN cat > /etc/apt/sources.list <<'EOF'
deb https://mirrors.tencent.com/debian/ bookworm main non-free non-free-firmware contrib
deb-src https://mirrors.tencent.com/debian/ bookworm main non-free non-free-firmware contrib
deb https://mirrors.tencent.com/debian-security/ bookworm-security main
deb-src https://mirrors.tencent.com/debian-security/ bookworm-security main
deb https://mirrors.tencent.com/debian/ bookworm-updates main non-free non-free-firmware contrib
deb-src https://mirrors.tencent.com/debian/ bookworm-updates main non-free non-free-firmware contrib
deb https://mirrors.tencent.com/debian/ bookworm-backports main non-free non-free-firmware contrib
deb-src https://mirrors.tencent.com/debian/ bookworm-backports main non-free non-free-firmware contrib
EOF

CMD ["bash"]