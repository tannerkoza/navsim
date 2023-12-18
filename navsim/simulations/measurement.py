import numpy as np
import pandas as pd
import pathlib as pl
import scipy.io as sio
import warnings

from tqdm import tqdm
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass

from navtools import get_signal_properties
from navtools.signals.signals import SatelliteSignal
from navtools.constants import SPEED_OF_LIGHT
from navsim.simulations.simulation import SignalSimulation
from navsim.emitters import SatelliteEmitters
from navsim.error_models import (
    get_ionosphere_model,
    get_troposphere_model,
    get_clock_allan_variance_values,
    compute_clock_states,
)
from navsim.error_models.signal import compute_carrier_to_noise
from navsim.error_models.atmosphere import (
    IonosphereModelParameters,
    TroposphereModelParameters,
)
from navsim.config import (
    SimulationConfiguration,
    TimeConfiguration,
    ConstellationsConfiguration,
    ErrorConfiguration,
)


# Simulation Outputs
@dataclass(frozen=True)
class ReceiverTruthStates:
    time: np.array
    pos: np.array
    vel: np.array
    clock_bias: np.array
    clock_drift: np.array


@dataclass(frozen=True)
class Observables:
    code_pseudorange: float
    carrier_pseudorange: float
    pseudorange_rate: float
    carrier_doppler: float
    cn0: float


@dataclass(frozen=True)
class Signals:
    properties: SatelliteSignal
    js: float


class MeasurementSimulation(SignalSimulation):
    def __init__(self, configuration: SimulationConfiguration) -> None:
        self.__init_time(configuration=configuration.time)
        self.__init_emitters(configuration=configuration.constellations)
        self.__init_errors(configuration=configuration.errors)
        self.__build_output_file_stem(configuration=configuration)

        self.__observables = []

        super().__init__()

    @property
    def emitter_states(self):
        print("[navsim] getting emitter truth states...")

        return self.__emitter_states

    @property
    def ephemerides(self):
        print("[navsim] getting emitter ephemerides...")

        return self.__ephemerides

    @property
    def rx_states(self):
        print("[navsim] getting receiver truth states...")

        return self.__rx_states

    @property
    def observables(self):
        print("[navsim] getting simulated observables...")

        return self.__observables

    @property
    def signal_properties(self):
        print("[navsim] getting signal properties...")

        return [signal.properties for signal in self.__signals.values()]

    def simulate(self, rx_pos: np.array, rx_vel: np.array = None):
        self.__rx_states = self.__simulate_receiver_states(rx_pos=rx_pos, rx_vel=rx_vel)
        self.__emitter_states, self.__ephemerides = self.__simulate_emitters(
            rx_pos=self.__rx_states.pos, rx_vel=self.__rx_states.vel
        )

        description = "[navsim] simulating observables"
        for period, emitters in tqdm(
            enumerate(self.__emitter_states), total=self.__nperiods, desc=description
        ):
            code_delays, carrier_delays, drifts = self.__compute_channel_delays(
                emitters=emitters, pos=self.__rx_states.pos[period]
            )
            observables = self.__compute_observables(
                emitters=emitters,
                code_delays=code_delays,
                carrier_delays=carrier_delays,
                drifts=drifts,
                clock_bias=self.__rx_states.clock_bias[period],
                clock_drift=self.__rx_states.clock_drift[period],
            )

            self.__observables.append(observables)

        print("[navsim] measurement simulation complete!")

    def to_hdf(self, output_dir_path: str):
        output_path = pl.Path(output_dir_path) / self.__output_file_stem

        emitter_states_df = pd.DataFrame(self.__emitter_states)
        rx_states_df = pd.DataFrame([self.__rx_states])
        observables_df = pd.DataFrame(self.__observables)

        warnings.simplefilter(action="ignore", category=pd.errors.PerformanceWarning)
        emitter_states_df.to_hdf(
            output_path.with_suffix(".h5"), key="emitter_states", mode="a"
        )
        rx_states_df.to_hdf(output_path.with_suffix(".h5"), key="rx_states", mode="a")
        observables_df.to_hdf(
            output_path.with_suffix(".h5"), key="observables", mode="a"
        )

        print(
            f"[navsim] exported measurement-level results to {output_path.with_suffix('.h5')}"
        )

    def to_mat(self, output_dir_path: str):
        output_path = pl.Path(output_dir_path) / self.__output_file_stem

        formatted_emitter_states = self.__reformat_for_mat_file(
            emitters_info=self.__emitter_states
        )
        formatted_observables = self.__reformat_for_mat_file(
            emitters_info=self.__observables
        )
        sio.savemat(
            file_name=output_path.with_suffix(".mat"),
            mdict={
                "emitter_states": formatted_emitter_states,
                "rx_states": self.__rx_states,
                "observables": formatted_observables,
            },
            do_compression=True,
        )

        print(
            f"[navsim] exported measurement-level results to {output_path.with_suffix('.mat')}"
        )

    def __init_time(self, configuration: TimeConfiguration):
        self.__duration = configuration.duration
        self.__tsim = 1 / configuration.fsim
        self.__nperiods = (
            int(np.ceil(configuration.duration / self.__tsim)) + 1
        )  # add one to account for duration

        self.__initial_time = datetime(
            year=configuration.year,
            month=configuration.month,
            day=configuration.day,
            hour=configuration.hour,
            minute=configuration.minute,
            second=configuration.second,
        )

    def __init_emitters(self, configuration: ConstellationsConfiguration):
        self.__emitters = SatelliteEmitters(
            constellations=configuration.emitters.keys(),
            mask_angle=configuration.mask_angle,
        )
        self.__signals = {
            constellation.casefold(): Signals(
                properties=get_signal_properties(signal_name=properties.get("signal")),
                js=properties.get("js"),
            )
            for constellation, properties in configuration.emitters.items()
        }

    def __init_errors(self, configuration: ErrorConfiguration):
        if configuration.ionosphere is None:
            self.__is_ionosphere_simulated = False
        else:
            self.__is_ionosphere_simulated = True
            self.__ionosphere = get_ionosphere_model(
                model_name=configuration.ionosphere
            )

        if configuration.troposphere is None:
            self.__is_troposphere_simulated = False
        else:
            self.__is_troposphere_simulated = True
            self.__troposphere = get_troposphere_model(
                model_name=configuration.troposphere
            )

        if configuration.rx_clock is None:
            self.__is_rx_clock_simulated = False
        else:
            self.__is_rx_clock_simulated = True
            self.__rx_clock = get_clock_allan_variance_values(
                clock_name=configuration.rx_clock
            )

        self.__pseudorange_awgn_sigma = configuration.pseudorange_awgn_sigma
        self.__carr_psr_awgn_sigma = configuration.carr_psr_awgn_sigma
        self.__pseudorange_rate_awgn_sigma = configuration.pseudorange_rate_awgn_sigma
            
        self.__is_atmosphere_drift_uninitialized = True

    def __simulate_receiver_states(self, rx_pos: np.array, rx_vel: np.array):
        # tiles rx_pos and rx_vel if static
        if rx_pos.size == 3:
            rx_pos = np.tile(rx_pos, (self.__nperiods, 1))
            rx_vel = np.zeros_like(rx_pos)
        else:
            rx_pos = rx_pos[: self.__nperiods, :]
            rx_vel = rx_vel[: self.__nperiods, :]

        if self.__is_rx_clock_simulated:
            clock_bias, clock_drift = compute_clock_states(
                h0=self.__rx_clock.h0,
                h2=self.__rx_clock.h2,
                T=self.__tsim,
                nperiods=self.__nperiods,
            )  # [m, m/s]
        else:
            clock_bias = np.zeros(self.__nperiods)
            clock_drift = np.zeros(self.__nperiods)

        self.__timeseries = np.linspace(
            start=0, stop=self.__duration, num=self.__nperiods
        )
        self.__datetime_series = [
            self.__initial_time + timedelta(0, time_step)
            for time_step in self.__timeseries
        ]

        states = ReceiverTruthStates(
            time=self.__timeseries,
            pos=rx_pos,
            vel=rx_vel,
            clock_bias=clock_bias,
            clock_drift=clock_drift,
        )

        return states

    def __simulate_emitters(self, rx_pos: np.array, rx_vel: np.array):
        emitter_states = self.__emitters.from_datetimes(
            datetimes=self.__datetime_series, rx_pos=rx_pos, rx_vel=rx_vel
        )
        ephemerides = self.__emitters.ephemerides()
        return emitter_states, ephemerides

    def __compute_channel_delays(self, emitters: dict, pos: float):
        code_delays = defaultdict()
        carrier_delays = defaultdict()
        drifts = defaultdict()

        # initializes delay values to calculate drift
        if self.__is_atmosphere_drift_uninitialized:
            self.__iono_delay = {emitter: 0.0 for emitter in emitters}
            self.__tropo_delay = {emitter: 0.0 for emitter in emitters}

            self.__is_atmosphere_drift_uninitialized = False

        # removes out of view and adds in view emitters
        self.__update_emitters_in_period(
            target_emitters=self.__iono_delay, updated_emitters=emitters
        )
        self.__update_emitters_in_period(
            target_emitters=self.__tropo_delay, updated_emitters=emitters
        )

        for emitter, state in emitters.items():
            if self.__is_ionosphere_simulated:
                signal = self.__signals.get(state.constellation.casefold())
                ionosphere_parameters = IonosphereModelParameters(
                    time=state.datetime,
                    rx_pos=pos,
                    emitter_pos=state.pos,
                    az=state.az,
                    el=state.el,
                    fcarrier=signal.properties.fcarrier,
                )

                new_iono_delay = self.__ionosphere.get_delay(
                    params=ionosphere_parameters
                )
                iono_drift = (new_iono_delay - self.__iono_delay[emitter]) / self.__tsim
            else:
                new_iono_delay = 0.0
                iono_drift = 0.0

            if self.__is_troposphere_simulated:
                troposphere_parameters = TroposphereModelParameters(
                    rx_pos=pos, el=state.el
                )

                new_tropo_delay = self.__troposphere.get_delay(
                    params=troposphere_parameters
                )
                tropo_drift = (
                    new_tropo_delay - self.__tropo_delay[emitter]
                ) / self.__tsim
            else:
                new_tropo_delay = 0.0
                tropo_drift = 0.0

            self.__iono_delay[emitter] = new_iono_delay
            self.__tropo_delay[emitter] = new_tropo_delay

            code_delays[emitter] = new_iono_delay + new_tropo_delay
            carrier_delays[emitter] = -new_iono_delay + new_tropo_delay
            drifts[emitter] = -iono_drift + tropo_drift

        return code_delays, carrier_delays, drifts

    def __compute_observables(
        self,
        emitters: dict,
        code_delays: dict,
        carrier_delays: dict,
        drifts: dict,
        clock_bias: float,
        clock_drift: float,
    ):
        observables = defaultdict()

        for emitter, state in emitters.items():
            signal = self.__signals.get(state.constellation.casefold())

            # observables do not include emitter clock terms
            code_pseudorange = state.range + code_delays[emitter] + clock_bias + self.__pseudorange_awgn_sigma * np.random.randn()
            carrier_pseudorange = state.range + carrier_delays[emitter] + clock_bias + self.__carr_psr_awgn_sigma * np.random.randn()
            pseudorange_rate = state.range_rate + drifts[emitter] + clock_drift + self.__pseudorange_rate_awgn_sigma * np.random.randn()
            carrier_doppler = (
                -pseudorange_rate * signal.properties.fcarrier / SPEED_OF_LIGHT
            )

            cn0 = compute_carrier_to_noise(
                range=state.range,
                transmit_power=signal.properties.transmit_power,
                transmit_antenna_gain=signal.properties.transmit_antenna_gain,
                fcarrier=signal.properties.fcarrier,
                js=signal.js,
            )

            emitter_observables = Observables(
                code_pseudorange=code_pseudorange,
                carrier_pseudorange=carrier_pseudorange,
                pseudorange_rate=pseudorange_rate,
                carrier_doppler=carrier_doppler,
                cn0=cn0,
            )
            observables[emitter] = emitter_observables

        return observables

    def __build_output_file_stem(self, configuration: SimulationConfiguration):
        now = datetime.now().strftime(format="%Y%m%d-%H%M%S")
        sim_date = self.__initial_time.strftime(format="%Y%m%d-%H%M%S")
        self.__output_file_stem = f"{now}_NAVSIM_{sim_date}_{int(configuration.time.duration)}_{configuration.time.fsim}"

    @staticmethod
    def __reformat_for_mat_file(emitters_info: list):
        formatted_emitter_states = []

        for emitters in emitters_info:
            new_emitters = defaultdict()

            for id, state in emitters.items():
                if not id.isalnum():
                    new_id = "".join(filter(str.isalnum, id))
                else:
                    new_id = id

                new_emitters[new_id] = emitters[id]

                if hasattr(state, "datetime"):
                    new_emitters[new_id].datetime = state.datetime.strftime(
                        format="%Y-%m-%d %H:%M:%S"
                    )

            formatted_emitter_states.append(new_emitters)

        return formatted_emitter_states

    @staticmethod
    def __update_emitters_in_period(target_emitters: dict, updated_emitters: dict):
        new_emitters = set(updated_emitters) - set(target_emitters)
        removed_emitters = set(target_emitters) - set(updated_emitters)

        for emitter in new_emitters:
            target_emitters[emitter] = 0.0

        for emitter in removed_emitters:
            target_emitters.pop(emitter)