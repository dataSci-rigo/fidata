# systemd/user/ — laptop-only user units

These are systemd **user** units for the laptop, NOT the VM. The parent
`systemd/` directory holds the VM's system units (pipeline + reviews);
nothing here should ever be deployed there — the discovery universe's only
consumer is the laptop's local viewer (`local_server.py` /discover page).

Install on the laptop:

    cp systemd/user/fidata-universe.* ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now fidata-universe.timer

So the Saturday 08:00 run fires even without an open session:

    sudo loginctl enable-linger ai1

`Persistent=true` catches up a missed run after suspend. The script loads
fiData/.env itself (dotenv), so no EnvironmentFile= is needed. Check with:

    systemctl --user list-timers fidata-universe.timer
    journalctl --user -u fidata-universe.service -n 50
