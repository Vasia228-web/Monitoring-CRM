#!/bin/bash
# Одноразове системне налаштування Fedora. Потребує пароля sudo — тому його
# запускає людина, а не агент:
#
#     sudo bash ~/realty/deploy/fedora/root-setup.sh
#
# Що робить (і нічого більше):
#   1. кришка ноутбука: закрита — система не засинає (logind);
#   2. сон вимкнено повністю: GNOME і екран входу самі призначають сон через
#      15–20 хв простою, і тоді не допоможе навіть п. 1;
#   3. служби користувача стартують при завантаженні, без входу в систему;
#   4. бібліотеки для Chromium НЕ ставимо: `ldd` показав, що їх уже вистачає.
set -euo pipefail

USER_NAME="${SUDO_USER:-u}"
if [ "$(id -u)" -ne 0 ]; then
  echo "Запустіть через sudo: sudo bash $0" >&2
  exit 1
fi

echo "1/3 Кришка: ignore (logind.conf.d/50-realty-lid.conf)"
install -d /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/50-realty-lid.conf <<'CONF'
# Realty: ноутбук працює як сервер — закрита кришка не означає сон.
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
CONF

echo "2/3 Сон заборонено (mask sleep/suspend/hibernate)"
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target \
  suspend-then-hibernate.target

echo "3/3 Автостарт служб користувача $USER_NAME без входу (linger)"
loginctl enable-linger "$USER_NAME"

# logind перечитує налаштування за SIGHUP; повністю все застосується після
# перезавантаження, яке однаково є частиною перевірки.
systemctl kill -s HUP systemd-logind || true

echo
echo "Перевірка:"
echo "  кришка:  $(busctl get-property org.freedesktop.login1 /org/freedesktop/login1 \
  org.freedesktop.login1.Manager HandleLidSwitch 2>/dev/null)"
echo "  сон:     $(systemctl is-enabled suspend.target 2>/dev/null || true)"
echo "  linger:  $(loginctl show-user "$USER_NAME" -p Linger --value)"
echo "Готово."
