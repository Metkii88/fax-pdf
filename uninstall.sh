#!/usr/bin/env bash
# uninstall.sh — удаление Fax-PDF/HylaFAX с Astra 1.7 (данные опционально)
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Запустите от root"; exit 1; }

for s in hylafax fax-xferfaxd fax-pdf-monitor; do
    systemctl disable --now "$s" 2>/dev/null || true
done
rm -f /etc/systemd/system/{hylafax,fax-xferfaxd,fax-pdf-monitor,hylafax-getty@}.service
systemctl daemon-reload

rm -rf /opt/hylafax /usr/local/bin/{faxq,faxgetty,sendfax,recvfax,xferfaxd,faxaddmodem,faxstat,textfmt} \
       /usr/local/sbin/faxq /opt/fax-pdf
read -rp "Удалить очередь и архивы (/var/spool/hylafax, /srv/fax, /var/log/fax-pdf)? [y/N] " a
[[ "${a,,}" == y ]] && rm -rf /var/spool/hylafax /srv/fax /var/log/fax-pdf

userdel faxq xferfaxd faxmon 2>/dev/null || true
groupdel fax 2>/dev/null || true
echo "Удаление завершено."
