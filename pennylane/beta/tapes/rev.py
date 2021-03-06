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
Quantum tape that implements reversible backpropagation.
"""
# pylint: disable=attribute-defined-outside-init,protected-access
from copy import copy
from functools import reduce
from string import ascii_letters as ABC

import numpy as np

import pennylane as qml

from .tape import QuantumTape


ABC_ARRAY = np.array(list(ABC))


class ReversibleTape(QuantumTape):
    r"""Quantum tape for computing gradients via reversible analytic differentiation.

    .. note::

        The reversible analytic differentation method has the following restrictions:

        * As it requires knowledge of the statevector, only statevector simulator devices can be used.

        * Differentiation is only supported for the parametrized quantum operations
          :class:`~.RX`, :class:`~.RY`, :class:`~.RZ`, and :class:`~.Rot`.

    This class extends the :class:`~.jacobian` method of the quantum tape to support analytic
    gradients of qubit operations using reversible analytic differentiation. This gradient method
    returns *exact* gradients, however requires use of a statevector simulator. Simply create
    the tape, and then call the Jacobian method:

    >>> tape.jacobian(dev)

    For more details on the quantum tape, please see :class:`~.QuantumTape`.

    **Reversible analytic differentiation**

    Assume a circuit has a gate :math:`G(\theta)` that we want to differentiate.
    Without loss of generality, we can write the circuit in the form three unitaries: :math:`UGV`.
    Starting from the initial state :math:`\vert 0\rangle`, the quantum state is evolved up to the
    "pre-measurement" state :math:`\vert\psi\rangle=UGV\vert 0\rangle`, which is saved
    (this can be reused for each variable being differentiated).

    We then apply the unitary :math:`V^{-1}` to evolve this state backwards in time
    until just after the gate :math:`G` (hence the name "reversible").
    The generator of :math:`G` is then applied as a gate, and we evolve forward using :math:`V` again.
    At this stage, the state of the simulator is proportional to
    :math:`\frac{\partial}{\partial\theta}\vert\psi\rangle`.
    Some further post-processing of this gives the derivative
    :math:`\frac{\partial}{\partial\theta} \langle \hat{O} \rangle` for any observable O.

    The reversible approach is similar to backpropagation, but trades off extra computation for
    enhanced memory efficiency. Where backpropagation caches the state tensors at each step during
    a forward pass, the reversible method only caches the final pre-measurement state.

    Compared to the parameter-shift rule, the reversible method can
    be faster or slower, depending on the density and location of parametrized gates in a circuit
    (circuits with higher density of parametrized gates near the end of the circuit will see a
    benefit).
    """

    def _grad_method(self, idx, use_graph=True, default_method="A"):
        return super()._grad_method(idx, use_graph=use_graph, default_method=default_method)

    @staticmethod
    def _matrix_elem(vec1, obs, vec2, device):
        r"""Computes the matrix element of an observable.

        That is, given two basis states :math:`\mathbf{i}`, :math:`\mathbf{j}`,
        this method returns :math:`\langle \mathbf{i} \vert \hat{O} \vert \mathbf{j} \rangle`.
        Unmeasured wires are contracted, and a scalar is returned.

        Args:
            vec1 (array[complex]): a length :math:`2^N` statevector
            obs (.Observable): a PennyLane observable
            vec2 (array[complex]): a length :math:`2^N` statevector
            device (.QubitDevice): the device used to compute the matrix elements
        """
        # pylint: disable=protected-access
        mat = device._reshape(obs.matrix, [2] * len(obs.wires) * 2)
        wires = obs.wires

        vec1_indices = ABC[: device.num_wires]

        obs_in_indices = "".join(ABC_ARRAY[wires.tolist()].tolist())
        obs_out_indices = ABC[device.num_wires : device.num_wires + len(wires)]
        obs_indices = "".join([obs_in_indices, obs_out_indices])

        vec2_indices = reduce(
            lambda old_string, idx_pair: old_string.replace(idx_pair[0], idx_pair[1]),
            zip(obs_in_indices, obs_out_indices),
            vec1_indices,
        )

        einsum_str = "{vec1_indices},{obs_indices},{vec2_indices}->".format(
            vec1_indices=vec1_indices,
            obs_indices=obs_indices,
            vec2_indices=vec2_indices,
        )

        return device._einsum(einsum_str, device._conj(vec1), mat, vec2)

    def jacobian(self, device, params=None, **options):
        # The parameter_shift_var method needs to evaluate the circuit
        # at the unshifted parameter values; the pre-rotated statevector is then stored
        # self._state attribute. Here, we set the value of the attribute to None
        # before each Jacobian call, so that the statevector is calculated only once.
        self._state = None
        return super().jacobian(device, params, **options)

    def analytic_pd(self, idx, device, params=None, **options):
        t_idx = list(self.trainable_params)[idx]
        op = self._par_info[t_idx]["op"]
        p_idx = self._par_info[t_idx]["p_idx"]

        # The reversible tape only support differentiating
        # expectation values of observables for now.
        for m in self.measurements:
            if (
                m.return_type is qml.operation.Variance
                or m.return_type is qml.operation.Probability
            ):
                raise ValueError(
                    f"{m.return_type} is not supported with the reversible gradient method"
                )

        # The reversible tape only supports the RX, RY, RZ, and Rot operations for now:
        #
        # * CRX, CRY, CRZ ops have a non-unitary matrix as generator.
        #
        # * PauliRot, MultiRZ, U2, and U3 do not have generators specified.
        #
        # TODO: the controlled rotations can be supported by multiplying ``state``
        # directly by these generators within this function
        # (or by allowing non-unitary matrix multiplies in the simulator backends)

        if op.name not in ["RX", "RY", "RZ", "Rot"]:
            raise ValueError(
                "The {} gate is not currently supported with the "
                "reversible gradient method.".format(op.name)
            )

        if self._state is None:
            self.execute_device(params, device)
            self._state = device._pre_rotated_state

        self.set_parameters(params)

        # create a new circuit which rewinds the pre-measurement state to just after `op`,
        # applies the generator of `op`, and then plays forward back to
        # pre-measurement step
        wires = op.wires
        op_idx = self.operations.index(op)

        # TODO: likely better to use circuitgraph to determine minimally necessary ops
        between_ops = self.operations[op_idx + 1 :]

        if op.name == "Rot":
            decomp = op.decomposition(*op.parameters, wires=wires)
            generator, multiplier = decomp[p_idx].generator
            between_ops = decomp[p_idx + 1 :] + between_ops
        else:
            generator, multiplier = op.generator

        generator = generator(wires)

        diff_circuit = QuantumTape()
        diff_circuit._ops = [copy(op).inv() for op in between_ops[::-1]] + [generator] + between_ops

        # set the simulator state to be the pre-measurement state
        device._state = self._state

        # evolve the pre-measurement state under this new circuit
        device.execute(diff_circuit)
        dstate = device._pre_rotated_state  # TODO: this will only work for QubitDevices

        # compute matrix element <d(state)|O|state> for each observable O
        matrix_elems = device._asarray(
            [self._matrix_elem(dstate, ob, self._state, device) for ob in self.observables]
            # TODO: if all observables act on same number of wires, could
            # do all at once with einsum
        )

        # reset state back to pre-measurement value
        device._pre_rotated_state = self._state

        return 2 * multiplier * device._imag(matrix_elems)
