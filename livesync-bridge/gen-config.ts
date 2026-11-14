// Erzeugt die livesync-bridge-Konfiguration beim Containerstart aus Umgebungs-
// variablen (aus vault.env). So liegt die E2EE-Passphrase nur in vault.env
// (chmod 600, nicht im Repo) und nie als config.json auf dem Host.
//
// Aufruf (im Container): deno run --allow-env --allow-write=/tmp gen-config.ts
// Ergebnis: /tmp/lsb-config.json, gelesen ueber LSB_CONFIG.

function req(name: string): string {
    const v = Deno.env.get(name);
    if (!v) {
        console.error(`[gen-config] ${name} fehlt in vault.env - Abbruch.`);
        Deno.exit(1);
    }
    return v;
}

const config = {
    peers: [
        {
            type: "couchdb",
            name: "vault-couchdb",
            group: "vault",
            url: req("VAULT_COUCHDB_URL"),
            database: req("VAULT_COUCHDB_DB"),
            username: req("VAULT_COUCHDB_USER"),
            password: req("VAULT_COUCHDB_PASSWORD"),
            passphrase: req("VAULT_PASSPHRASE"),
            // Pfadverschleierung ist im Vault AUS (Tweak usePathObfuscation=false).
            obfuscatePassphrase: "",
            // Uebernimmt E2EE v2, v3-rabin-karp, xxhash64, Chunkgroessen 60/20 und
            // case-insensitive Dateinamen aus _local/obsydian_livesync_milestone.
            useRemoteTweaks: true,
            baseDir: "",
        },
        {
            type: "storage",
            name: "vault-files",
            group: "vault",
            // Muss unter data/ liegen (Volume /app/data).
            baseDir: "data/vault/",
            scanOfflineChanges: true,
            // Obsidian-Interna und Papierkorb nicht zurueck in die DB schieben.
            ignore: ["**/.obsidian/**", "**/.trash/**"],
        },
    ],
};

await Deno.writeTextFile("/tmp/lsb-config.json", JSON.stringify(config), { mode: 0o600 });
console.log(`[gen-config] /tmp/lsb-config.json geschrieben (CouchDB: ${config.peers[0].url}/${config.peers[0].database}).`);
