from unittest.mock import Mock

from django.test import SimpleTestCase

from apps.business import jidanpu
from jidanpu.errors import ApiError


class RepairLookupTests(SimpleTestCase):
    window = {"stime": "2026-10-01 00:00:00", "etime": "2026-10-03 23:59:59"}
    remote_id = "SYNTH-JDP-REPAIR-ORDER"

    def test_rejected_empty_result_is_not_confirmed_absent(self):
        for recycle in (False, True):
            with self.subTest(recycle=recycle):
                client = Mock()
                client.call.return_value = []
                client.last_envelope = {"code": 0, "msg": "查询日期错误", "count": 0}
                with self.assertRaises(ApiError):
                    jidanpu.find_order_for_repair(
                        client, remote_order_id=self.remote_id, window=self.window, recycle=recycle,
                    )

    def test_recycle_search_reads_next_page_and_matches_internal_id(self):
        client = Mock()
        first_page = [{"orderId": f"SYNTH-OTHER-{i}"} for i in range(20)]
        match = {"orderId": self.remote_id, "orderSta": "已退单"}
        client.call.side_effect = [first_page, [match]]
        client.last_envelope = {"code": 0, "msg": "", "count": 21}

        result = jidanpu.find_order_for_repair(
            client, remote_order_id=self.remote_id, window=self.window, recycle=True,
        )

        self.assertEqual(result, match)
        self.assertEqual(
            [call.kwargs["query"]["page"] for call in client.call.call_args_list], [1, 2],
        )
        for call in client.call.call_args_list:
            self.assertEqual(call.args, ("GET", "/OrderAjax/GetOrderRecycleList"))
            self.assertEqual(call.kwargs["query"]["key1"], self.remote_id)

    def test_source_search_does_not_accept_another_order_with_the_same_source(self):
        client = Mock()
        client.call.return_value = [{"orderId": "SYNTH-OTHER", "orderIdView": "SYNTH-SOURCE"}]
        client.last_envelope = {"code": 0, "msg": "", "count": 1}
        self.assertIsNone(jidanpu.find_order_for_repair(
            client, remote_order_id=self.remote_id, window=self.window,
            source_order_no="SYNTH-SOURCE",
        ))
        form = client.call.call_args.kwargs["data"]
        self.assertEqual((form["minPrice"], form["maxPrice"]), ("", ""))

    def test_restore_and_source_change_write_to_the_same_internal_id(self):
        client = Mock()
        jidanpu.restore_recycled_order(client, remote_order_id=self.remote_id)
        jidanpu.change_source_order_no(
            client, remote_order_id=self.remote_id, source_order_no="SYNTH-SOURCE-NEW",
        )
        restore, change = client.call.call_args_list
        self.assertEqual(restore.args, ("POST", "/OrderAjax/RefOrderRecycle"))
        self.assertEqual(restore.kwargs["data"], {"orderId": self.remote_id})
        self.assertEqual(change.args, ("POST", "/OrderAjax/ChangeOrderSoureId"))
        self.assertEqual(change.kwargs["data"], {
            "orderId": self.remote_id, "value": "SYNTH-SOURCE-NEW",
        })
