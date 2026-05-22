from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class GraphNodeSpec(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    image_name: str = "cirros-base.img"
    vcpus: int = Field(default=1, ge=1, le=8)
    ram_mb: int = Field(default=256, ge=128, le=32768)
    disk_gb: int = Field(default=10, ge=2, le=200)
    preferred_worker: Optional[str] = None
    internet: bool = False


class GraphLinkSpec(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    from_node: str = Field(alias="from")
    to_node: str = Field(alias="to")

    model_config = {
        "populate_by_name": True,
    }

    @model_validator(mode="after")
    def validate_endpoints(self):
        if self.from_node == self.to_node:
            raise ValueError("Un enlace no puede conectar un nodo consigo mismo")
        return self


class GraphSliceCreateRequest(BaseModel):
    slice_name: str = Field(min_length=1, max_length=100)
    vlan_base: int = Field(ge=100, le=3990)
    vnc_start: int = Field(default=5901, ge=5901, le=65000)
    network_backend: Literal["vlan", "vxlan"] = "vlan"
    internet_mode: Literal["none", "headnode_nat", "provider_network"] = "none"
    nodes: list[GraphNodeSpec]
    links: list[GraphLinkSpec]

    @model_validator(mode="after")
    def validate_graph(self):
        if len(self.nodes) < 2:
            raise ValueError("La topología debe tener al menos 2 nodos")

        if len(self.links) < 1:
            raise ValueError("La topología debe tener al menos 1 enlace")

        node_names = [n.name for n in self.nodes]
        if len(node_names) != len(set(node_names)):
            raise ValueError("Los nombres de nodos deben ser únicos")

        link_ids = [l.id for l in self.links]
        if len(link_ids) != len(set(link_ids)):
            raise ValueError("Los IDs de enlaces deben ser únicos")

        node_set = set(node_names)
        for link in self.links:
            if link.from_node not in node_set:
                raise ValueError(f"El enlace {link.id} usa un nodo inexistente: {link.from_node}")
            if link.to_node not in node_set:
                raise ValueError(f"El enlace {link.id} usa un nodo inexistente: {link.to_node}")

        if self.internet_mode in {"headnode_nat", "provider_network"} and not any(n.internet for n in self.nodes):
            raise ValueError("Con internet_mode=headnode_nat o provider_network al menos un nodo debe tener internet=true")

        return self
