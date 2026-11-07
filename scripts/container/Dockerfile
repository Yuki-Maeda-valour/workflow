# 全回帰テスト(python3 -B scripts/run-all.py)を流す容器のイメージ。
# 入口は scripts/run-all-container.sh(Dockerfile と requirements-dev.txt の中身の値を、イメージの名前に入れる)。
# 作り方は、2026-11-06 に使っていたイメージ(workflow-test:jammy-gitppa-md)を docker history・docker inspect で
# 読み取ったものと同じにする。git は PPA の最新(版は固定できない。流した版は PR の本文の「流した環境」に残す)。
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8

RUN apt-get update \
 && apt-get install -y --no-install-recommends software-properties-common ca-certificates gpg-agent \
 && add-apt-repository -y ppa:git-core/ppa \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      git python3 python3-pip procps lsof file xxd jq bsdextrautils util-linux coreutils findutils grep sed gawk diffutils \
 && rm -rf /var/lib/apt/lists/*

# root で、システムの置き場(/usr/local/lib/python3.10/dist-packages)に入れる。利用者別の置き場には入れない
# (利用者別の依存を読む順序を確かめるテストがあるため)。
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements-dev.txt \
 && rm /tmp/requirements-dev.txt

RUN useradd --create-home --uid 1000 --shell /bin/bash tester

USER tester
# ~/.cache を tester の持ち物で作っておく(docker run で ~/.cache/workflow-run-all を取り付けても、
# 親の ~/.cache が root の持ち物で作られて、書けなくならないように)。
RUN git config --global user.name tester \
 && git config --global user.email tester@example.invalid \
 && git config --global init.defaultBranch main \
 && mkdir -p /home/tester/.cache
WORKDIR /home/tester
