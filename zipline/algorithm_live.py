#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from datetime import time
import os.path
import logbook
import pandas as pd

import zipline.protocol as zp
from zipline.algorithm import TradingAlgorithm
from zipline.assets._assets import Asset
from zipline.gens.realtimeclock import RealtimeClock
from zipline.gens.tradesimulation import AlgorithmSimulator
from zipline.errors import (OrderInBeforeTradingStart,
                            ScheduleFunctionOutsideTradingStart)
from zipline.utils.input_validation import error_keywords
from zipline.utils.api_support import (
    ZiplineAPI,
    api_method,
    disallowed_in_before_trading_start,
    allowed_only_in_before_trading_start)

from zipline.utils.calendars.trading_calendar import days_at_time
from zipline.utils.serialization_utils import load_context, store_context

log = logbook.Logger("Live Trading")


class LiveAlgorithmExecutor(AlgorithmSimulator):
    def __init__(self, *args, **kwargs):
        super(self.__class__, self).__init__(*args, **kwargs)


class LiveTradingAlgorithm(TradingAlgorithm):
    def __init__(self, *args, **kwargs):
        self.broker = kwargs.pop('broker', None)
        self.orders = {}

        self.algo_filename = kwargs.get('algo_filename', "<algorithm>")
        self.state_filename = kwargs.pop('state_filename', None)
        self._context_persistence_excludes = []

        super(self.__class__, self).__init__(*args, **kwargs)

        log.info("initialization done")

    def initialize(self, *args, **kwargs):
        self._context_persistence_excludes = (list(self.__dict__.keys()) +
                                              ['trading_client'])

        if os.path.isfile(self.state_filename):
            log.info("Loading state from {}".format(self.state_filename))
            load_context(self.state_filename,
                         context=self,
                         checksum=self.algo_filename)
            return

        with ZiplineAPI(self):
            super(self.__class__, self).initialize(*args, **kwargs)
            store_context(self.state_filename,
                          context=self,
                          checksum=self.algo_filename,
                          exclude_list=self._context_persistence_excludes)

    def handle_data(self, data):
        super(self.__class__, self).handle_data(data)
        store_context(self.state_filename,
                      context=self,
                      checksum=self.algo_filename,
                      exclude_list=self._context_persistence_excludes)

    def _create_clock(self):
        # This method is taken from TradingAlgorithm.
        # The clock has been replaced to use RealtimeClock
        trading_o_and_c = self.trading_calendar.schedule.ix[
            self.sim_params.sessions]
        assert self.sim_params.emission_rate == 'minute'

        minutely_emission = True
        market_opens = trading_o_and_c['market_open']
        market_closes = trading_o_and_c['market_close']

        # The calendar's execution times are the minutes over which we actually
        # want to run the clock. Typically the execution times simply adhere to
        # the market open and close times. In the case of the futures calendar,
        # for example, we only want to simulate over a subset of the full 24
        # hour calendar, so the execution times dictate a market open time of
        # 6:31am US/Eastern and a close of 5:00pm US/Eastern.
        execution_opens = \
            self.trading_calendar.execution_time_from_open(market_opens)
        execution_closes = \
            self.trading_calendar.execution_time_from_close(market_closes)

        # FIXME generalize these values
        before_trading_start_minutes = days_at_time(
            self.sim_params.sessions,
            time(8, 45),
            "US/Eastern"
        )

        return RealtimeClock(
            self.sim_params.sessions,
            execution_opens,
            execution_closes,
            before_trading_start_minutes,
            minute_emission=minutely_emission,
            time_skew=self.broker.time_skew
        )

    def _create_generator(self, sim_params):
        # Call the simulation trading algorithm for side-effects:
        # it creates the perf tracker
        TradingAlgorithm._create_generator(self, sim_params)
        self.trading_client = LiveAlgorithmExecutor(
            self,
            sim_params,
            self.data_portal,
            self._create_clock(),
            self._create_benchmark_source(),
            self.restrictions,
            universe_func=self._calculate_universe
        )

        return self.trading_client.transform()

    def updated_portfolio(self):
        return self.broker.portfolio

    def updated_account(self):
        return self.broker.account

    @api_method
    @allowed_only_in_before_trading_start(
        ScheduleFunctionOutsideTradingStart())
    def schedule_function(self,
                          func,
                          date_rule=None,
                          time_rule=None,
                          half_days=True,
                          calendar=None):
        # If the scheduled_function() is called from initalize()
        # then the state persistence would need to take care of storing and
        # restoring the scheduled functions too (as initialize() only called
        # once in the algorithm's life). Persisting scheduled functions are
        # difficult as they are not serializable by default.
        # We enforce scheduled functions to be called only from
        # before_trading_start() in live trading with a decorator.
        super(self.__class__, self).schedule_function(func,
                                                      date_rule,
                                                      time_rule,
                                                      half_days,
                                                      calendar)

    @api_method
    def symbol(self, symbol_str):
        # This method works around the problem of not being able to trade
        # assets which does not have ingested data for the day of trade.
        # Normally historical data is loaded to bundle and the asset's
        # end_date and auto_close_date is set based on the last entry from
        # the bundle db. LiveTradingAlgorithm does not override order_value(),
        # order_percent() & order_target(). Those higher level ordering
        # functions provide a safety net to not to trade de-listed assets.
        # If the asset is returned as it was ingested (end_date=yesterday)
        # then CannotOrderDelistedAsset exception will be raised from the
        # higher level order functions.
        #
        # Hence, we are increasing the asset's end_date by 10,000 days.
        # The ample buffer is provided for two reasons:
        # 1) assets are often stored in algo's context through initialize(),
        #    which is called once and persisted at live trading. 10,000 days
        #    enables 27+ years of trading, which is more than enough.
        # 2) Tool - 10,000 Days is brilliant!

        asset = super(self.__class__, self).symbol(symbol_str)
        tradeable_asset = asset.to_dict()
        tradeable_asset['end_date'] = (pd.Timestamp('now', tz='UTC') +
                                       pd.Timedelta('10000 days'))
        tradeable_asset['auto_close_date'] = tradeable_asset['end_date']
        return Asset.from_dict(tradeable_asset)

    @api_method
    @disallowed_in_before_trading_start(OrderInBeforeTradingStart())
    def order(self,
              asset,
              amount,
              limit_price=None,
              stop_price=None,
              style=None):
        amount, style = self._calculate_order(asset, amount,
                                              limit_price, stop_price, style)

        return self.broker.order(asset, amount, limit_price, stop_price, style)

    @api_method
    def batch_market_order(self, share_counts):
        raise NotImplementedError()

    @error_keywords(sid='Keyword argument `sid` is no longer supported for '
                        'get_open_orders. Use `asset` instead.')
    @api_method
    def get_open_orders(self, asset=None):
        return self.broker.get_open_orders(asset)

    @api_method
    def get_order(self, order_id):
        return self.broker.get_order(order_id)

    @api_method
    def cancel_order(self, order_param):
        order_id = order_param
        if isinstance(order_param, zp.Order):
            order_id = order_param.id
        self.broker.cancel_order(order_id)
