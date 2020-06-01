# Copyright 2018-2020 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
This module contains the :class:`QubitDevice` abstract base class.
"""

# For now, arguments may be different from the signatures provided in Device
# e.g. instead of expval(self, observable, wires, par) have expval(self, observable)
# pylint: disable=arguments-differ, abstract-method, no-value-for-parameter,too-many-instance-attributes
import abc
import itertools

import numpy as np

from pennylane.operation import Sample, Variance, Expectation, Probability
from pennylane.qnodes import QuantumFunctionError
from pennylane import Device


class QubitDevice(Device):
    """Abstract base class for PennyLane qubit devices.

    The following abstract method **must** be defined:

    * :meth:`~.apply`: append circuit operations, compile the circuit (if applicable),
      and perform the quantum computation.

    Devices that generate their own samples (such as hardware) may optionally
    overwrite :meth:`~.probabilty`. This method otherwise automatically
    computes the probabilities from the generated samples, and **must**
    overwrite the following method:

    * :meth:`~.generate_samples`: Generate samples from the device from the
      exact or approximate probability distribution.

    Analytic devices **must** overwrite the following method:

    * :meth:`~.analytic_probability`: returns the probability or marginal probability from the
      device after circuit execution. :meth:`~.marginal_prob` may be used here.

    This device contains common utility methods for qubit-based devices. These
    do not need to be overwritten. Utility methods include:

    * :meth:`~.expval`, :meth:`~.var`, :meth:`~.sample`: return expectation values,
      variances, and samples of observables after the circuit has been rotated
      into the observable eigenbasis.

    Args:
        wires (int): number of subsystems in the quantum state represented by the device
        shots (int): number of circuit evaluations/random samples used to estimate
            expectation values of observables
        analytic (bool): If ``True``, the device calculates probability, expectation values,
            and variances analytically. If ``False``, a finite number of samples set by
            the argument ``shots`` are used to estimate these quantities.
    """

    # pylint: disable=too-many-public-methods
    _asarray = staticmethod(np.asarray)
    observables = {"PauliX", "PauliY", "PauliZ", "Hadamard", "Hermitian", "Identity"}

    def __init__(self, wires=1, shots=1000, analytic=True):
        super().__init__(wires=wires, shots=shots)

        self.analytic = analytic
        """bool: If ``True``, the device supports exact calculation of expectation
        values, variances, and probabilities. If ``False``, samples are used
        to estimate the statistical quantities above."""

        self._samples = None
        """None or array[int]: stores the samples generated by the device
        *after* rotation to diagonalize the observables."""

        self._circuit_hash = None
        """None or int: stores the hash of the circuit from the last execution which
        can be used by devices in :meth:`apply` for parametric compilation."""

    @classmethod
    def capabilities(cls):
        """Get the capabilities of the plugin.

        Capabilities include:

        * ``"model"`` (*str*): either ``"qubit"`` or ``"CV"``.

        * ``"inverse_operations"`` (*bool*): ``True`` if the device supports
          applying the inverse of operations. Operations which should be inverted
          have ``operation.inverse == True``.

        * ``"tensor_observables" (*bool*): ``True`` if the device supports
          expectation values/variance/samples of :class:`~.Tensor` observables.

        The qubit device class has built-in support for tensor observables. As a
        result, devices that inherit from this class automatically
        have the following items in their capabilities
        dictionary:

        * ``"model": "qubit"``
        * ``"tensor_observables": True``

        Returns:
            dict[str->*]: results
        """
        capabilities = cls._capabilities
        capabilities.update(model="qubit", tensor_observables=True)
        return capabilities

    def reset(self):
        """Reset the backend state.

        After the reset, the backend should be as if it was just constructed.
        Most importantly the quantum state is reset to its initial value.
        """
        self._samples = None
        self._circuit_hash = None

    def execute(self, circuit, **kwargs):
        """Execute a queue of quantum operations on the device and then
        measure the given observables.

        For plugin developers: instead of overwriting this, consider
        implementing a suitable subset of

        * :meth:`apply`

        * :meth:`~.generate_samples`

        * :meth:`~.probability`

        Additional keyword arguments may be passed to the this method
        that can be utilised by :meth:`apply`. An example would be passing
        the ``QNode`` hash that can be used later for parametric compilation.

        Args:
            circuit (~.CircuitGraph): circuit to execute on the device

        Raises:
            QuantumFunctionError: if the value of :attr:`~.Observable.return_type` is not supported

        Returns:
            array[float]: measured value(s)
        """
        self.check_validity(circuit.operations, circuit.observables)

        self._circuit_hash = circuit.hash

        # apply all circuit operations
        self.apply(circuit.operations, rotations=circuit.diagonalizing_gates, **kwargs)

        # generate computational basis samples
        if (not self.analytic) or circuit.is_sampled:
            self._samples = self.generate_samples()

        # compute the required statistics
        results = self.statistics(circuit.observables)

        # Ensures that a combination with sample does not put
        # expvals and vars in superfluous arrays
        all_sampled = all(obs.return_type is Sample for obs in circuit.observables)
        if circuit.is_sampled and not all_sampled:
            return self._asarray(results, dtype="object")

        return self._asarray(results)

    @abc.abstractmethod
    def apply(self, operations, **kwargs):
        """Apply quantum operations, rotate the circuit into the measurement
        basis, and compile and execute the quantum circuit.

        This method receives a list of quantum operations queued by the QNode,
        and should be responsible for:

        * Constructing the quantum program
        * (Optional) Rotating the quantum circuit using the rotation
          operations provided. This diagonalizes the circuit so that arbitrary
          observables can be measured in the computational basis.
        * Compile the circuit
        * Execute the quantum circuit

        Both arguments are provided as lists of PennyLane :class:`~.Operation`
        instances. Useful properties include :attr:`~.Operation.name`,
        :attr:`~.Operation.wires`, and :attr:`~.Operation.parameters`,
        and :attr:`~.Operation.inverse`:

        >>> op = qml.RX(0.2, wires=[0])
        >>> op.name # returns the operation name
        "RX"
        >>> op.wires # returns the Wires
        [0]
        >>> op.parameters # returns a list of parameters
        [0.2]
        >>> op.inverse # check if the operation should be inverted
        False
        >>> op = qml.RX(0.2, wires=[0]).inv
        >>> op.inverse
        True

        Args:
            operations (list[~.Operation]): operations to apply to the device

        Keyword args:
            rotations (list[~.Operation]): operations that rotate the circuit
                pre-measurement into the eigenbasis of the observables.
            hash (int): the hash value of the circuit constructed by `CircuitGraph.hash`
        """

    @staticmethod
    def active_wires(operators):
        """Returns the wires acted on by a set of operators.

        Args:
            operators (list[~.Operation]): operators for which
                we are gathering the active wires

        Returns:
            set[int]: the set of wires activated by the specified operators
        """
        wires = []
        for op in operators:
            wires.extend(op.wires.tolist())

        return set(wires)

    def statistics(self, observables):
        """Process measurement results from circuit execution and return statistics.

        This includes returning expectation values, variance, samples and probabilities.

        Args:
            observables (List[:class:`Observable`]): the observables to be measured

        Raises:
            QuantumFunctionError: if the value of :attr:`~.Observable.return_type` is not supported

        Returns:
            Union[float, List[float]]: the corresponding statistics
        """
        results = []

        for obs in observables:
            # Pass instances directly
            if obs.return_type is Expectation:
                results.append(self.expval(obs))

            elif obs.return_type is Variance:
                results.append(self.var(obs))

            elif obs.return_type is Sample:
                results.append(np.array(self.sample(obs)))

            elif obs.return_type is Probability:
                results.append(self.probability(wires=self.translate(obs.wires)))

            elif obs.return_type is not None:
                raise QuantumFunctionError(
                    "Unsupported return type specified for observable {}".format(obs.name)
                )

        return results

    def generate_samples(self):
        r"""Returns the computational basis samples generated for all wires.

        Note that PennyLane uses the convention :math:`|q_0,q_1,\dots,q_{N-1}\rangle` where
        :math:`q_0` is the most significant bit.

        .. warning::

            This method should be overwritten on devices that
            generate their own computational basis samples, with the resulting
            computational basis samples stored as ``self._samples``.

        Returns:
             array[complex]: array of samples in the shape ``(dev.shots, dev.num_wires)``
        """
        number_of_states = 2 ** self.num_wires

        rotated_prob = self.analytic_probability()

        samples = self.sample_basis_states(number_of_states, rotated_prob)
        return QubitDevice.states_to_binary(samples, self.num_wires)

    def sample_basis_states(self, number_of_states, state_probability):
        """Sample from the computational basis states based on the state
        probability.

        This is an auxiliary method to the generate_samples method.

        Args:
            number_of_states (int): the number of basis states to sample from

        Returns:
            List[int]: the sampled basis states
        """
        basis_states = np.arange(number_of_states)
        return np.random.choice(basis_states, self.shots, p=state_probability)

    @staticmethod
    def states_to_binary(samples, num_wires):
        """Convert basis states from base 10 to binary representation.

        This is an auxiliary method to the generate_samples method.

        Args:
            samples (List[int]): samples of basis states in base 10 representation
            number_of_states (int): the number of basis states to sample from

        Returns:
            List[int]: basis states in binary representation
        """
        powers_of_two = 1 << np.arange(num_wires)
        states_sampled_base_ten = samples[:, None] & powers_of_two
        return (states_sampled_base_ten > 0).astype(int)[:, ::-1]

    @property
    def circuit_hash(self):
        """The hash of the circuit upon the last execution.

        This can be used by devices in :meth:`~.apply` for parametric compilation.
        """
        return self._circuit_hash

    @property
    def state(self):
        """Returns the state vector of the circuit prior to measurement.

        .. note::

            Only state vector simulators support this property. Please see the
            plugin documentation for more details.
        """
        raise NotImplementedError

    def analytic_probability(self, wires=None):
        r"""Return the (marginal) probability of each computational basis
        state from the last run of the device.

        PennyLane uses the convention
        :math:`|q_0,q_1,\dots,q_{N-1}\rangle` where :math:`q_0` is the most
        significant bit.

        If no wires are specified, then all the basis states representable by
        the device are considered and no marginalization takes place.

        .. note::

            :meth:`marginal_prob` may be used as a utility method
            to calculate the marginal probability distribution.

        Args:
            wires (Sequence[int]): Sequence of wires to return
                marginal probabilities for. Wires not provided
                are traced out of the system.

        Returns:
            List[float]: list of the probabilities
        """
        raise NotImplementedError

    def estimate_probability(self, wires=None):
        """Return the estimated probability of each computational basis state
        using the generated samples.

        Args:
            wires (Sequence[int]): Sequence of wires to return
                marginal probabilities for. Wires not provided
                are traced out of the system.

        Returns:
            List[float]: list of the probabilities
        """
        # consider only the requested wires
        wires = np.hstack(wires)

        samples = self._samples[:, np.array(wires)]  # TODO: Use indices for nonconsec wires

        # convert samples from a list of 0, 1 integers, to base 10 representation
        unraveled_indices = [2] * len(wires)
        indices = np.ravel_multi_index(samples.T, unraveled_indices)

        # count the basis state occurrences, and construct the probability vector
        basis_states, counts = np.unique(indices, return_counts=True)
        prob = np.zeros([2 ** len(wires)], dtype=np.float64)
        prob[basis_states] = counts / self.shots
        return prob

    def probability(self, wires=None):
        """Return either the analytic probability or estimated probability of
        each computational basis state.

        If no :attr:`~.analytic` attributes exists for the device, then return the
        estimated probability.

        Args:
            wires (Sequence[int]): Sequence of wires to return
                marginal probabilities for. Wires not provided
                are traced out of the system.

        Returns:
            List[float]: list of the probabilities
        """
        wires = wires or range(self.num_wires)

        if hasattr(self, "analytic") and self.analytic:
            return self.analytic_probability(wires=wires)

        return self.estimate_probability(wires=wires)

    def marginal_prob(self, prob, wires=None):
        r"""Return the marginal probability of the computational basis
        states by summing the probabiliites on the non-specified wires.

        If no wires are specified, then all the basis states representable by
        the device are considered and no marginalization takes place.

        .. note::

            If the provided wires are not strictly increasing, the returned marginal
            probabilities take this permuation into account.

            For example, if ``wires=[2, 0]``, then the returned marginal
            probability vector will take this 'reversal' of the two wires
            into account:

            .. math::

                \mathbb{P}^{(2, 0)} = \[ |00\rangle, |10\rangle, |01\rangle, |11\rangle\]

        Args:
            prob: The probabilities to return the marginal probabilities
                for
            wires (Sequence[int]): Sequence of wires to return
                marginal probabilities for. Wires not provided
                are traced out of the system.

        Returns:
            array[float]: array of the resulting marginal probabilities.
        """
        if wires is None:
            # no need to marginalize
            return prob

        wires = np.hstack(wires)  # TODO: nonconsecutive

        # determine which wires are to be summed over
        inactive_wires = list(set(range(self.num_wires)) - set(wires))

        # reshape the probability so that each axis corresponds to a wire
        prob = prob.reshape([2] * self.num_wires)

        # sum over all inactive wires
        prob = np.apply_over_axes(np.sum, prob, inactive_wires).flatten()

        # The wires provided might not be in consecutive order (i.e., wires might be [2, 0]).
        # If this is the case, we must permute the marginalized probability so that
        # it corresponds to the orders of the wires passed.
        basis_states = np.array(list(itertools.product([0, 1], repeat=len(wires))))
        perm = np.ravel_multi_index(
            basis_states[:, np.argsort(np.argsort(wires))].T, [2] * len(wires)
        )
        return prob[perm]

    def expval(self, observable):
        wires = self.translate(observable.wires)

        if self.analytic:
            # exact expectation value
            eigvals = observable.eigvals
            prob = self.probability(wires=wires)
            return (eigvals @ prob).real

        # estimate the ev
        return np.mean(self.sample(observable))

    def var(self, observable):
        wires = self.translate(observable.wires)

        if self.analytic:
            # exact variance value
            eigvals = observable.eigvals
            prob = self.probability(wires=wires)
            return (eigvals ** 2) @ prob - (eigvals @ prob).real ** 2

        # estimate the variance
        return np.var(self.sample(observable))

    def sample(self, observable):
        wires = self.translate(observable.wires)
        name = observable.name

        if isinstance(name, str) and name in {"PauliX", "PauliY", "PauliZ", "Hadamard"}:
            # Process samples for observables with eigenvalues {1, -1}
            return 1 - 2 * self._samples[:, wires[0]]

        # Replace the basis state in the computational basis with the correct eigenvalue.
        # Extract only the columns of the basis samples required based on ``wires``.
        wires = np.hstack(wires)
        samples = self._samples[:, np.array(wires)]
        unraveled_indices = [2] * len(wires)
        indices = np.ravel_multi_index(samples.T, unraveled_indices)
        return observable.eigvals[indices]
