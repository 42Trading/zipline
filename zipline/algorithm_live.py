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

from zipline.algorithm import TradingAlgorithm
from zipline.gens.realtimeclock import RealtimeClock
from zipline.gens.tradesimulation import AlgorithmSimulator
from zipline.errors import OrderInBeforeTradingStart
from zipline.utils.api_support import (
    ZiplineAPI,
    api_method,
    disallowed_in_before_trading_start)

from zipline.utils.calendars.trading_calendar import days_at_time
from zipline.utils.persistence import persist_state, unpersist_state

log = logbook.Logger("Live Trading")


class LiveAlgorithmExecutor(AlgorithmSimulator):
    def __init__(self, *args, **kwargs):
        super(self.__class__, self).__init__(*args, **kwargs)


class LiveTradingAlgorithm(TradingAlgorithm):
    def __init__(self, *args, **kwargs):
        self.live_trading = kwargs.pop('live_trading', False)
        self.broker = kwargs.pop('broker', None)

        super(self.__class__, self).__init__(*args, **kwargs)

        self._state_file_path = "state.p"  #TODO: get a name
        self._fields_prior = []

        log.info("initialization done")

    def initialize(self, *args, **kwargs):
        """
        Overrides default initialize
        """

        # check if state persistance exists, if not call initialize
        if not os.path.isfile(self._state_file_path):
            log.info("no state file found, calling initialize")
            with ZiplineAPI(self):
                fields_prior = self.__dict__.keys()
                self._initialize(self, *args, **kwargs)
                persist_state(self._state_file_path, self, fields_prior)
        else:
            log.info("state file found, loading state from persistence store")
            unpersist_state(self._state_file_path, self)

    def handle_data(self, data):
        super.handle_data(self, data)
        log.info("handle data has been called, writing state to persistence store")
        persist_state(self._state_file_path, self, self._fields_prior)

    def _create_clock(self):
        # This method is taken from TradingAlgorithm.
        # The clock has been replaced to use RealtimeClock
        trading_o_and_c = self.trading_calendar.schedule.ix[
            self.sim_params.sessions]
        market_closes = trading_o_and_c['market_close']
        minutely_emission = False

        if self.sim_params.data_frequency == 'minute':
            market_opens = trading_o_and_c['market_open']

            minutely_emission = self.sim_params.emission_rate == "minute"
        else:
            # in daily mode, we want to have one bar per session, timestamped
            # as the last minute of the session.
            market_opens = market_closes

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
    @disallowed_in_before_trading_start(OrderInBeforeTradingStart())
    def order(self,
              asset,
              amount,
              limit_price=None,
              stop_price=None,
              style=None):
        raise NotImplementedError()

    @api_method
    def batch_market_order(self, share_counts):
        raise NotImplementedError()

    def get_open_orders(self, asset=None):
        raise NotImplementedError()

    def get_order(self, order_id):
        raise NotImplementedError()

    def cancel_order(self, order_param):
        raise NotImplementedError()
