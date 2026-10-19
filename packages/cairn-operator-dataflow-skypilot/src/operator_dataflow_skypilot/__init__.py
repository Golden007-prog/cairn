"""cairn-operator-dataflow-skypilot — the SkyPilot provisioning dataflow engine (ADR-0104 D5),
an optional wheel offered by ``cairndev[skypilot]`` (ADR-0134 D3). Registered as ``skypilot``
in the ``cairn.dataflow_engines`` group; core selects it by name and never imports this
package, and asks its ``check_ready()`` at the launch door (ADR-0134 D3c)."""
