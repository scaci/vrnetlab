# dnlab/opnsense

OPNsense come immagine vrnetlab DNLab-native, partendo dalla **serial pre-installed image** ufficiale (`OPNsense-X.Y-serial-amd64.img.bz2`).

Niente installer interattivo, niente pexpect: l'immagine è già installata e già configurata per la console seriale, quindi il build si limita a:

1. decomprimere il `.bz2`,
2. convertire da `raw` a `qcow2` (`qemu-img convert`),
3. espandere a 8 GB (`qemu-img resize`),
4. impacchettare in un'immagine Docker che esegue `launch.py`.

A runtime parte un overlay qcow2 sopra la base immutabile, e — se l'utente monta un volume su `/persist` — l'overlay finisce lì e sopravvive a `docker restart`.

## Requisiti host

- `docker`
- `qemu-utils` (`bunzip2` è in `bzip2`, di solito già installato)
- `/dev/kvm` esposto al container a runtime (raccomandato; fallback TCG molto più lento)

## Build

```bash
cd /opt/vrnetlab/dnlab/opnsense
# Mettere l'immagine OPNsense ufficiale in questa directory:
cp /path/a/OPNsense-26.1.6-serial-amd64.img.bz2 .
make
```

La versione viene estratta dal nome del file. Al termine vengono prodotti due tag (alias):

```
vrnetlab/dnlab_opnsense:26.1.6
vrnetlab/dnlab_opnsense:26.1.6-dnlab
```

Per buildare una versione/tag custom:

```bash
make IMAGE_REPOSITORY=miorepo/opnsense
make TAG_SUFFIX=        # build senza alias "-dnlab"
```

## Run (effimero)

```bash
docker run --rm -it --name opn \
  --device /dev/kvm \
  -p 5000:5000 \
  vrnetlab/dnlab_opnsense:26.1.6-dnlab

telnet localhost 5000
# entro ~60 s appare il prompt OPNsense
# login: root / opnsense
```

## Run con persistenza

```bash
mkdir -p persist
docker run --rm -d --name opn \
  --device /dev/kvm \
  -p 5000:5000 \
  -v "$PWD/persist:/persist" \
  vrnetlab/dnlab_opnsense:26.1.6-dnlab

# fai modifiche via :5000 (hostname, interfacce, regole), poi:
docker restart opn
# riconnetti via :5000 e verifica che le modifiche siano sopravvissute

ls persist/
# base.qcow2          → symlink a /installed.qcow2 (base immutabile dentro il container)
# base-overlay.qcow2  → overlay reale, contiene tutte le modifiche
```

Per resettare lo stato è sufficiente cancellare `base-overlay.qcow2`.

## Default

| | |
|---|---|
| RAM | 2048 MB (override via `QEMU_MEMORY=...`) |
| vCPU | 1 (override via `QEMU_SMP=...`) |
| NIC | 8 virtio-net-pci (vtnet0=mgmt su `pci.1`) |
| Console seriale | TCP/5000 (telnet) |
| Username / password upstream | `root` / `opnsense` |

## Override di configurazione

In questa prima versione non è disponibile un meccanismo dichiarativo per iniettare `config.xml`. Per personalizzare:
- accedi via console seriale (`telnet :5000`), oppure
- abilita l'interfaccia web/SSH dal menu console, e
- usa il volume `/persist` per rendere le modifiche durature.

Un'iniezione automatica di `config.xml` (config-drive ISO + `rc.d/dnlab-bootstrap`) è in roadmap per la v2.

## Limiti noti

- Solo `amd64`.
- Senza `/dev/kvm` l'immagine fa boot in TCG ma estremamente lenta — non è la modalità d'uso prevista.
- L'override `config.xml` non è ancora implementato (v2).
