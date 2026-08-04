"""Graph mode: the rules the application enforces in Python."""

from __future__ import annotations

import unittest
from itertools import count

from pydantic import ValidationError

import src.mcp_server.orders as orders
from src.mcp_server.orders import (
    GraphUser,
    OrderItem,
    OrderStatus,
    create_order_for,
    get_order_for,
    identity_proof_for,
    list_orders_for,
    update_order_status_for,
)


class OrderTests(unittest.TestCase):
    def setUp(self) -> None:
        orders._orders.clear()
        orders._order_numbers = count(1)

    def test_create_calculates_total_and_lists_by_owner(self) -> None:
        order = create_order_for(
            "user-a",
            "Ada",
            [
                OrderItem(sku="ABC-1", quantity=2, unit_price_cents=1250),
                OrderItem(sku="XYZ_2", quantity=1, unit_price_cents=499),
            ],
        )

        self.assertEqual(order.order_id, "ORD-000001")
        self.assertEqual(order.total_cents, 2999)
        self.assertEqual(list_orders_for("user-a"), [order])
        self.assertEqual(list_orders_for("user-b"), [])

    def test_owner_isolation_hides_existing_order(self) -> None:
        order = create_order_for(
            "user-a",
            "Ada",
            [OrderItem(sku="ABC", quantity=1, unit_price_cents=100)],
        )

        with self.assertRaisesRegex(ValueError, "Order not found"):
            get_order_for("user-b", order.order_id)

    def test_valid_status_sequence_and_idempotency(self) -> None:
        order = create_order_for(
            "user-a",
            "Ada",
            [OrderItem(sku="ABC", quantity=1, unit_price_cents=100)],
        )

        processing = update_order_status_for(
            "user-a", order.order_id, OrderStatus.PROCESSING
        )
        self.assertIs(
            update_order_status_for("user-a", order.order_id, OrderStatus.PROCESSING),
            processing,
        )
        update_order_status_for("user-a", order.order_id, OrderStatus.SHIPPED)
        delivered = update_order_status_for(
            "user-a", order.order_id, OrderStatus.DELIVERED
        )
        self.assertEqual(delivered.status, OrderStatus.DELIVERED)

    def test_invalid_status_transition_fails(self) -> None:
        order = create_order_for(
            "user-a",
            "Ada",
            [OrderItem(sku="ABC", quantity=1, unit_price_cents=100)],
        )

        with self.assertRaisesRegex(
            ValueError, "Cannot move an order from pending to shipped"
        ):
            update_order_status_for("user-a", order.order_id, OrderStatus.SHIPPED)

    def test_item_validation_rejects_invalid_values(self) -> None:
        invalid_items = (
            {"sku": "spaces are invalid", "quantity": 1, "unit_price_cents": 1},
            {"sku": "ABC", "quantity": 0, "unit_price_cents": 1},
            {"sku": "ABC", "quantity": 1, "unit_price_cents": -1},
        )
        for item in invalid_items:
            with self.subTest(item=item), self.assertRaises(ValidationError):
                OrderItem.model_validate(item)

        with self.assertRaisesRegex(ValueError, "at least one item"):
            create_order_for("user-a", "Ada", [])

    def test_identity_proof_is_stable_without_exposing_graph_id(self) -> None:
        user = GraphUser(
            id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            displayName="Ada",
        )
        proof = identity_proof_for(user)
        self.assertEqual(proof.display_name, "Ada")
        self.assertEqual(proof, identity_proof_for(user))
        self.assertEqual(len(proof.identity_fingerprint), 8)
        self.assertNotIn(user.id, proof.model_dump_json())
        self.assertEqual(proof.evidence, "Microsoft Graph /me via OBO")
