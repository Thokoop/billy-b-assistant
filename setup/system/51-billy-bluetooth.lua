-- WirePlumber 0.4 (Raspberry Pi OS Bookworm).
-- Role names describe the LOCAL machine: Billy is the A2DP sink and HFP
-- hands-free unit. Verified on hardware against the adapter UUID list.
bluez_monitor.properties["bluez5.roles"] = "[ a2dp_sink hfp_hf ]"
bluez_monitor.properties["bluez5.hfphsp-backend"] = "native"
bluez_monitor.properties["bluez5.enable-msbc"] = true
bluez_monitor.properties["with-logind"] = false
table.insert(bluez_monitor.rules, {
  matches = { { { "node.name", "matches", "bluez_input.*" } } },
  apply_properties = { ["bluez5.media-source-role"] = "input" },
})
