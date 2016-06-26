__author__ = 'vicident'

import operator
import pandas as pd
import sqlite3
from datetime import datetime
import pytz

import logging
from fx_session import MarketSession
from calendar import monthrange
import uuid

LONDON_TZ = pytz.timezone('Europe/London')

class FxBroker:

    ORDERS_COLUMNS = ['TYPE', 'OPEN_TIME', 'OPEN_PRICE', 'VOLUME', 'SL_PRICE', 'TP_PRICE']
    SELL_ORDER = 1
    BUY_ORDER = -1
    DB_TABLES = {'TIME': 0, 'OPEN_PRICE': 1, 'MIN_PRICE': 2, 'MAX_PRICE': 3, 'CLOSE_PRICE': 4, 'VOLUME': 5}

    def __init__(self, db_folder, db_list, pair_name, session, start_volume, slippage):
        data_frames_list = []
        self.sessions = []
        for db_name in db_list:
            db, sess = FxBroker.__load_tables("/".join([db_folder, db_name]), pair_name, session)
            data_frames_list.append(db)
            self.sessions += sess
        self.db = pd.concat(data_frames_list)
        self.sessions_num = len(self.sessions)
        self.session_len = max([b-a for a, b in self.sessions])
        logging.info("max session length: {0}".format(self.session_len))
        self.db_pointer = 0
        self.session_pointer = 0
        # fin data
        self.start_volume = start_volume
        self.orders_table = pd.DataFrame(columns=FxBroker.ORDERS_COLUMNS)
        self.volume = start_volume
        self.slippage = slippage

# public methods

    def get_frame_width(self):
        return len(FxBroker.DB_TABLES)

    def get_sessions_num(self):
        return len(self.sessions)

    def get_equity(self):
        spot = self._get_spot()
        op_col = self.orders_table['OPEN_PRICE']
        sign_col = self.orders_table['TYPE']
        vol_col = self.orders_table['VOLUME']
        order_profits = ((op_col - spot).multiply(sign_col) + 1.0 - self.slippage).multiply(vol_col)

        return order_profits.sum() + self.volume

    def get_volume(self):
        return self.volume

    def get_start_volume(self):
        return self.start_volume

    def update_orders(self, close):
        spot = self._get_spot()

        if close:
            # close all
            logging.debug("market state:\n" + self.get_orders_snapshot().to_string())
            call_orders = self.orders_table
        else:
            # choose take profit and stop loss
            call_orders = self.orders_table[(self.orders_table['SL_PRICE'] >= spot) |
                                        (self.orders_table['TP_PRICE'] <= spot)]
        op_col = call_orders['OPEN_PRICE']
        sign_col = call_orders['TYPE']
        vol_col = call_orders['VOLUME']
        order_profits = (op_col - spot).multiply(sign_col) - self.slippage
        profit_loss = order_profits.multiply(vol_col).sum()

        self.orders_table = self.orders_table.drop(call_orders.index)
        self.volume += (order_profits + 1.0).multiply(vol_col).sum()
        # profit given by orders
        return profit_loss

    def get_orders_snapshot(self):
        snapshot = self.orders_table.copy()
        for index, row in snapshot.iterrows():
            if row['TYPE'] == FxBroker.BUY_ORDER:
                snapshot['TYPE'].loc[index] = 'buy'
            elif row['TYPE'] == FxBroker.SELL_ORDER:
                snapshot['TYPE'].loc[index] = 'sell'
            sec = snapshot['OPEN_TIME'].loc[index] / 1000.0
            london_dt = LONDON_TZ.localize(datetime.utcfromtimestamp(sec), is_dst=None)
            snapshot['OPEN_TIME'].loc[index] = london_dt.strftime('%Y-%m-%d %H:%M')
        return snapshot

    def close_order(self, index):
        pass

# methods for inheritors

    def _reset(self):
        self.volume = self.start_volume
        self.orders_table = pd.DataFrame(columns=FxBroker.ORDERS_COLUMNS)

    def _get_spot(self):
        return self.db.iloc[self.db_pointer, FxBroker.DB_TABLES['CLOSE_PRICE']]

    def _get_time(self):
        return self.db.iloc[self.db_pointer, FxBroker.DB_TABLES['TIME']]

    def _get_volume(self):
        return self.db.iloc[self.db_pointer, FxBroker.DB_TABLES['VOLUME']]

    def _add_order(self, order_type, lot, sl_rate, tp_rate):
        if lot <= self.volume:
            spot = self._get_spot()
            open_time = self._get_time()
            index = uuid.uuid4()
            self.orders_table.loc[index] = [order_type, open_time, spot, lot, spot*(1 - sl_rate), spot*(1 + tp_rate)]
            self.volume -= lot
            return index
        else:
            return None

    def _go_next_random_session(self, seed, frame_len):
        start_session = frame_len / self.session_len + 1
        self.session_pointer = seed % self.sessions_num
        if self.session_pointer < start_session:
            self.session_pointer = start_session
        logging.info("go to random session: {0}".format(self.session_pointer))
        self.db_pointer = self.sessions[self.session_pointer][0]

    def _go_next_session(self, frame_len):
        start_session = frame_len / self.session_len + 1
        if self.session_pointer < self.sessions_num - 1:
            self.session_pointer += 1
        else:
            self.session_pointer = start_session
        self.db_pointer = self.sessions[self.session_pointer][0]

    def _go_first_session(self, frame_len):
        start_session = frame_len / self.session_len + 1
        self.session_pointer = start_session
        self.db_pointer = self.sessions[self.session_pointer][0]

    def _go_next_frame(self):
        if self.db_pointer < self.sessions[self.session_pointer][1] - 1:
            self.db_pointer += 1
            last_frame_flag = False
        else:
            last_frame_flag = True

        return last_frame_flag

    def _get_frame(self, frame_len):
        frame = self.db[(self.db_pointer - frame_len):self.db_pointer]
        return frame

# private methods

    @staticmethod
    def __load_tables(db_path, pair_name, session):
        """
        :param db_path: path to historical data
        :param pair_name: name of currency exchange pair
        :return:
        """
        table_names_pairs = sorted(FxBroker.DB_TABLES.items(), key=operator.itemgetter(1))
        logging.info("Loading " + db_path)
        # connect to sqlite database
        con = sqlite3.connect(db_path)
        # fetch table names
        cursor = con.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        cdata = cursor.fetchall()
        table_names = [c[0] for c in cdata]
        # check database
        for key, _ in table_names_pairs:
            if key not in table_names and key is not 'TIME':
                raise LookupError("Loaded database doesn't have required table: " + key)
        # read tables data to data frames (pandas)
        df_list = [pd.read_sql_query("SELECT TIME from " + table_names[0], con)]
        df_columns = []
        for key, _ in table_names_pairs:
            if key is not 'TIME':
                df_list.append(pd.read_sql_query("SELECT " + pair_name + " from " + key, con))
                logging.info(key + " has been read")
            df_columns.append(key)
        con.close()

        df = pd.concat(df_list, axis=1)
        df.columns = df_columns
        stop = False
        base_pointer = 0
        sessions = []
        while not stop:
            time_point_ms = df.ix[base_pointer, 'TIME']
            # get current day
            day = MarketSession.ms_to_datetime(time_point_ms)
            # get session's time limits for current day
            begin_ms, end_ms = session.get_session_range(day)
            # find time limits in database
            begin_ids = df[df['TIME'] == begin_ms].index.tolist()
            end_ids = df[df['TIME'] == end_ms + 1].index.tolist()
            # there should be only single time point per each limit
            if len(begin_ids) == 1 and len(end_ids) == 1:
                interval_len = end_ids[0] - begin_ids[0]
                volumes = df.ix[(interval_len/4 + begin_ids[0]):(3*interval_len/4 + begin_ids[0]), 'VOLUME']
                prices = df.ix[(interval_len/4 + begin_ids[0]):(3*interval_len/4 + begin_ids[0]), 'CLOSE_PRICE']
                # check real trading activity
                if volumes.sum() and prices.max() != prices.min():
                    sessions.append((begin_ids[0], end_ids[0] - 1))

            _, max_days = monthrange(day.year, day.month)

            for i in xrange(1, max_days + 1):
                next_point_ids = df[df['TIME'] == begin_ms + session.get_session_period()*i].index.tolist()
                if len(next_point_ids) == 1:
                    base_pointer = next_point_ids[0]
                    stop = False
                    break
                else:
                    stop = True

        return df, sessions


class FxBroker2orders(FxBroker):

    def __init__(self, db_folder, db_list, frame_len, pair_name, session, start_volume, lot,
                 sl_rate=0.01, tp_rate=0.03, lose_rate=0.5, slippage=0.005):

        FxBroker.__init__(self, db_folder, db_list, pair_name, session, start_volume, slippage)
        self.frame_len = frame_len
        self.sl_rate = sl_rate
        self.tp_rate = tp_rate
        self.lose_rate = lose_rate
        self.lot = lot
        self.actions = {
            0: self.__nop,
            1: self.__buy,
            2: self.__sell
        }
        self._go_first_session(self.frame_len)

    def __nop(self):
        pass

    def __buy(self):
        self._add_order(FxBroker.BUY_ORDER, self.lot, self.sl_rate, self.tp_rate)

    def __sell(self):
        self._add_order(FxBroker.SELL_ORDER, self.lot, self.sl_rate, self.tp_rate)

    def reset(self, seed):
        self._reset()
        self._go_next_random_session(seed, self.frame_len)
        return self._get_frame(self.frame_len)

    def step(self, action, seed):
        frame = self._get_frame(self.frame_len)
        session_over = self._go_next_frame()
        reward = self.update_orders(session_over)

        if session_over:
            volume = self.get_volume()
            logging.debug("session has been closed with volume = " + str(volume))

        # game is over if we have lost a much
        game_over = float(self.get_equity()) / float(self.get_start_volume()) < self.lose_rate

        # make an action if continue playing
        if not game_over:
            self.actions[action]()
        else:
            reward = self.update_orders(True)
        # go to next session
        if session_over and not game_over:
            self._go_next_random_session(seed, self.frame_len)

        return frame, reward, game_over, session_over

    def get_actions_num(self):
        return len(self.actions.keys())
