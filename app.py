"""Usage:  solara run app.py """
import solara  # Import Solara for the reactive web interface.
from mesa import Agent, Model, DataCollector  # Import Mesa agents, models, and metric collection.
from mesa.space import MultiGrid  # Import the non-wrapping grid used for the city.
import matplotlib.pyplot as plt  # Import Matplotlib for simulation charts.


def manhattan(a, b):  # Distance between two districts on a block-shaped city.
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def on_shortest_path(start, end, point):  # True if `point` lies on some shortest Manhattan path from start to end.
    return manhattan(start, point) + manhattan(point, end) == manhattan(start, end)


def is_passenger(agent):  # Small helpers keep the metric definitions readable.
    return isinstance(agent, Passenger)


def is_taxi(agent):
    return isinstance(agent, Taxi)


class Passenger(Agent):  # Represent a passenger requesting a taxi ride.
    def __init__(self, model, origin, destination):  # Initialize one passenger.
        super().__init__(model)  # Register the passenger with the Mesa model.
        self.origin = origin  # Store the passenger's starting grid coordinate.
        self.destination = destination  # Store the passenger's destination coordinate.
        self.direct_distance = abs(destination[0] - origin[0]) + abs(destination[1] - origin[1])  # Calculate baseline direct trip distance for fuel-saving comparison.
        self.status = "waiting"  # Track whether the passenger is waiting, negotiating, awaiting_confirm, waiting_pickup, in_taxi, arrived, or impatient.
        self.wait_time = 0  # Count simulation steps spent waiting before boarding.
        self.travel_time = 0  # Count simulation steps spent riding inside a taxi.
        self.max_patience = self.random.randint(18, 35)  # Give the passenger a seeded random patience limit for acceptability.
        self.mailbox = []  # Store proposals and confirmations addressed to this passenger.
        self.assigned_taxi = None  # Store the taxi selected by the passenger.
        self.walk_progress = 0  # Count steps spent walking toward the next district (walking is slower than driving).
        self.reached_on_foot = False  # Record whether an impatient passenger eventually reached the destination on foot.

    def give_up(self, reason):  # Switch the passenger to walking and log why.
        """Turns the passenger into a pedestrian: the taxi service has lost this customer."""
        self.status = "impatient"  # Mark the passenger as unwilling to wait longer.
        self.model.log_event(f"⚠️ [IMPATIENT] Passenger #{self.unique_id} at {self.pos} {reason}; walking to {self.destination}")  # Log the impatience event.

    def step(self):  # Advance the passenger by one simulation step.
        if self.status == "waiting":  # Start a request when the passenger is waiting.
            self.wait_time += 1  # Increase the passenger's waiting time.
            if self.wait_time > self.max_patience:  # Check whether patience has run out.
                self.give_up("gave up waiting")  # Leave the taxi system.
                return  # Stop this passenger's turn immediately.

            # --- CFP PROTOCOL: STEP 1 (Call For Proposal) ---
            self.model.log_event(f"🚀 [CFP] Passenger #{self.unique_id} at {self.pos} broadcasting call to destination {self.destination}")  # Log the outgoing request.

            cfp_msg = {  # Build the call-for-proposal message.
                "sender": self,  # Identify the passenger sending the request.
                "type": "CFP",  # Label the message as a call for proposal.
                "origin": self.pos,  # Include the requested pickup location.
                "destination": self.destination  # Include the requested drop-off location.
            }  # Finish the CFP message.
            for agent in self.model.agents:  # Inspect every agent in the model.
                if isinstance(agent, Taxi):  # Send the request only to taxis.
                    agent.mailbox.append(cfp_msg)  # Put the CFP in the taxi's inbox.

            self.model.cfp_count_step += 1  # Increment the step counter for CFP broadcasts.
            self.status = "negotiating"  # Wait for taxi proposals on the next turn.

        elif self.status == "negotiating":  # Evaluate proposals received from taxis.
            self.wait_time += 1  # Count negotiation step toward total waiting time.
            # --- CFP PROTOCOL: STEP 3 (Evaluate Answers & Accept) ---
            best_taxi = None  # Hold the taxi offering the lowest bid score.
            min_bid = float('inf')  # Start with an effectively infinite distance/bid.

            for msg in self.mailbox:  # Examine every message waiting for this passenger.
                if msg["type"] == "PROPOSE" and msg["passenger"] == self:  # Keep proposals intended for this passenger.
                    if msg["bid_score"] < min_bid:  # Prefer the closest/lowest-detour taxi proposal.
                        min_bid = msg["bid_score"]  # Record the best bid score found so far.
                        best_taxi = msg["taxi"]  # Record the taxi making that proposal.

            self.mailbox.clear()  # Remove processed proposals from the inbox.

            if best_taxi:  # Accept a proposal when at least one taxi responded.
                best_taxi.mailbox.append({  # Send an acceptance message to that taxi.
                    "sender": self,  # Identify the passenger accepting the offer.
                    "type": "ACCEPT",  # Label the message as an acceptance.
                    "passenger": self  # Include the passenger being assigned.
                })  # Finish the acceptance message.
                self.model.accept_count_step += 1  # Increment the step counter for accepted proposals.
                self.model.log_event(f"✅ [ACCEPT] Passenger #{self.unique_id} accepted proposal from Taxi #{best_taxi.unique_id} at {best_taxi.pos} (Bid: {min_bid})")  # Log the accepted offer.
                self.status = "awaiting_confirm"  # Wait for the taxi to confirm seat availability.
            else:  # Handle a negotiation round with no proposals.
                if self.wait_time > self.max_patience:  # Check if patience expired while negotiating.
                    self.give_up("received no proposal in time")  # Leave the taxi system.
                else:  # Otherwise remain eligible to retry.
                    self.status = "waiting"  # Allow the passenger to broadcast again on the next step.

        elif self.status == "awaiting_confirm":  # Process seat confirmation from the chosen taxi.
            self.wait_time += 1  # Increase wait time while waiting for confirmation.
            confirmed = False  # Track whether a confirmation message was received.
            for msg in self.mailbox:  # Inspect every message in the passenger's inbox.
                if msg["type"] == "CONFIRM" and msg["passenger"] == self:  # Check for a positive seat confirmation.
                    self.assigned_taxi = msg["taxi"]  # Remember the confirmed taxi.
                    self.status = "waiting_pickup"  # Wait at origin while the taxi travels to pick up.
                    self.model.log_event(f"🤝 [CONFIRM] Taxi #{msg['taxi'].unique_id} confirmed pickup for Passenger #{self.unique_id} at {self.pos}")  # Log the confirmation.
                    confirmed = True  # Mark confirmation as succeeded.
                    break  # Stop scanning further messages.
                elif msg["type"] == "REJECT" and msg["passenger"] == self:  # Check if the taxi became full before confirming.
                    self.model.log_event(f"❌ [REJECT] Taxi #{msg['taxi'].unique_id} full; Passenger #{self.unique_id} re-entering CFP queue")  # Log the rejection.
                    self.status = "waiting"  # Return to waiting state to broadcast a new CFP.
            self.mailbox.clear()  # Clear processed confirmation or rejection messages.
            if not confirmed and self.status == "awaiting_confirm":  # Fallback if no message arrived.
                self.status = "waiting"  # Re-enter waiting state so the passenger never gets stuck.

        elif self.status == "waiting_pickup":  # Wait at the origin district while the assigned taxi is en route.
            self.wait_time += 1  # Continue counting waiting time until physical boarding.
            # A confirmed passenger is a little more patient (pickup_grace extra steps), but not infinitely:
            # if the taxi is still not there, the passenger cancels the booking and walks.
            if self.wait_time > self.max_patience + self.model.pickup_grace:  # Check whether even the extended patience is exhausted.
                if self.assigned_taxi is not None:  # Tell the booked taxi so it frees the seat.
                    self.assigned_taxi.mailbox.append({"type": "CANCEL", "sender": self, "passenger": self})  # Send a CANCEL message (protocol extension).
                    self.model.log_event(f"🚫 [CANCEL] Passenger #{self.unique_id} cancelled booking with Taxi #{self.assigned_taxi.unique_id}")  # Log the cancellation.
                self.assigned_taxi = None  # Forget the cancelled booking.
                self.give_up("cancelled after a late pickup")  # Leave the taxi system.

        elif self.status == "in_taxi":  # Track time while the passenger is riding inside the cab.
            self.travel_time += 1  # Increase the passenger's in-vehicle travel time.

        elif self.status == "impatient":  # Model pedestrian movement when a passenger gives up on taxis.
            if self.pos == self.destination:  # Check whether the walker has reached the destination.
                if not self.reached_on_foot:  # Record the arrival only once.
                    self.reached_on_foot = True  # Count this passenger as having arrived on foot.
                    self.model.log_event(f"🚶 [ON FOOT] Passenger #{self.unique_id} reached {self.destination} on foot")  # Log the arrival on foot.
                return  # Nothing more to do for this passenger.
            self.walk_progress += 1  # Spend one more step walking through the current district.
            if self.walk_progress >= self.model.walk_steps_per_cell:  # Move only after crossing the district at walking speed.
                self.walk_progress = 0  # Restart the counter for the next district.
                curr_x, curr_y = self.pos  # Read current passenger coordinates.
                dest_x, dest_y = self.destination  # Read target destination coordinates.
                next_x = curr_x + (1 if dest_x > curr_x else (-1 if dest_x < curr_x else 0))  # Step horizontally toward destination.
                next_y = curr_y if next_x != curr_x else curr_y + (1 if dest_y > curr_y else (-1 if dest_y < curr_y else 0))  # Step vertically if horizontally aligned.
                self.model.grid.move_agent(self, (next_x, next_y))  # Move the walking passenger on the MultiGrid.


class Taxi(Agent):  # Represent a taxi that negotiates, picks up, and transports passengers.
    def __init__(self, model, capacity=3, allow_route_change=True):  # Initialize one taxi.
        super().__init__(model)  # Register the taxi with the Mesa model.
        self.capacity = capacity  # Set the maximum number of simultaneous passengers.
        self.allow_route_change = allow_route_change  # Configure whether multi-hop detours are permitted.
        self.passengers = []  # Store passengers currently physically aboard the taxi.
        self.assigned_pickups = []  # Store passengers confirmed for pickup that the taxi is traveling to collect.
        self.route_queue = []  # Store the ordered multi-hop list of pickup and drop-off coordinates.
        self.distance_traveled = 0  # Track the taxi's total traveled Manhattan distance (fuel / pollution burned).
        self.mailbox = []  # Store CFP and acceptance messages for this taxi.

        # Abstract jumping state variables
        self.state = "idle"  # Track whether the taxi is idle or traveling.
        self.target_pos = None  # Store the destination of the current jump leg.
        self.trip_duration = 0  # Store the number of steps needed for the current trip leg.
        self.time_in_transit = 0  # Count steps elapsed during the current trip leg.

    @property
    def total_committed(self):  # Calculate total seats currently occupied or reserved for pickup.
        return len(self.passengers) + len(self.assigned_pickups)  # Sum aboard passengers and scheduled pickups.

    def is_route_compatible(self, passenger):  # Check whether a passenger fits the current carshare route and saves fuel.
        """Checks if sharing the ride with this passenger aligns with the route and saves fuel."""  # Document the route check.
        if self.total_committed >= self.capacity:  # Reject requests when all seats are occupied or reserved.
            return False  # Signal that no more passengers fit.
        if self.total_committed == 0:  # Accept any destination when the taxi is completely uncommitted.
            return True  # An empty taxi has no route constraints.

        ref_passenger = self.passengers[0] if self.passengers else self.assigned_pickups[0]  # Choose the primary passenger defining the route.
        primary_dest = ref_passenger.destination  # Use the primary passenger's destination as the main route.

        if not self.allow_route_change:  # Strict no-detour mode (Incremental Stage 2).
            # "No route change" = the new pickup and drop-off both lie on a shortest path the taxi already drives,
            # so the total distance of the trip does not increase at all.
            route_start = ref_passenger.origin if ref_passenger in self.assigned_pickups else (self.target_pos or self.pos)  # Where the primary route starts.
            pickup_on_route = on_shortest_path(route_start, primary_dest, passenger.pos)  # Is the new pickup on the way?
            drop_on_route = on_shortest_path(passenger.pos, primary_dest, passenger.destination)  # Is the new drop-off before the primary one, on the way?
            return pickup_on_route and drop_on_route  # Accept only zero-detour sharing.

        anchor = self.target_pos if self.target_pos else self.pos  # Read the taxi's current reference coordinates.
        pickup_dist = manhattan(passenger.pos, anchor)  # Distance to pick up the new carshare passenger.
        drop_gap = manhattan(passenger.destination, primary_dest)  # Distance between the two drop-offs.

        # Multi-hop sharing is accepted only if the extra detour (pickup + drop-off gap) stays within max_detour districts,
        # because beyond that two separate solo trips would usually cost less fuel than one shared trip.
        return (pickup_dist + drop_gap) <= self.model.max_detour  # Compare the detour with the configurable limit.

    def schedule_stop(self, coord):  # Add a pickup or drop-off stop to the multi-hop queue.
        """Inserts a target coordinate into the route queue sorted by proximity."""  # Document the stop scheduler.
        if coord not in self.route_queue and coord != self.target_pos:  # Avoid duplicate stops in the queue.
            self.route_queue.append(coord)  # Append the new stop coordinate.
            ref = self.target_pos if self.target_pos else self.pos  # Use current target or position as sorting anchor.
            self.route_queue.sort(key=lambda c: abs(c[0] - ref[0]) + abs(c[1] - ref[1]))  # Sort stops by Manhattan distance for multi-hop efficiency.

    def start_trip(self, target, extra_delay=0):  # Begin a jump trip toward a destination coordinate.
        """Sets up abstract jumping travel duration based on distance and delays."""  # Document the trip setup.
        self.target_pos = target  # Store the trip destination.
        current_x, current_y = self.pos  # Read the taxi's current location.
        target_x, target_y = target  # Read the target location.
        distance = abs(target_x - current_x) + abs(target_y - current_y)  # Calculate Manhattan distance.
        taxis_in_cell = sum(1 for a in self.model.grid.get_cell_list_contents([self.pos]) if isinstance(a, Taxi))  # Count taxis in the current district.
        traffic_delay = 1 if taxis_in_cell > 2 else 0  # Add a 1-step traffic jam delay if more than 2 taxis occupy the cell.
        self.trip_duration = max(1, distance + extra_delay + traffic_delay)  # Ensure trip takes at least one step plus any boarding/traffic delays.
        self.time_in_transit = 0  # Reset the elapsed trip time counter.
        self.state = "traveling"  # Mark the taxi as busy with this jump leg.

    def handle_arrival_at_current_pos(self):  # Process drop-offs and pickups at the taxi's current cell.
        """Drops off arrived passengers and boards confirmed passengers at current cell."""  # Document arrival handling.
        boarded_new = False  # Track whether any new passenger boarded during this stop.
        for p in self.passengers[:]:  # Iterate over a copy of aboard passengers so they can be removed safely.
            if self.pos == p.destination:  # Check whether this passenger has reached their destination.
                p.status = "arrived"  # Mark the passenger as successfully delivered.
                self.model.grid.move_agent(p, self.pos)  # Update the passenger's position on the MultiGrid.
                self.passengers.remove(p)  # Remove the passenger from the taxi.
                self.model.log_event(f"🏁 [ARRIVED] Taxi #{self.unique_id} dropped off Passenger #{p.unique_id} at {self.pos}")  # Log the drop-off.

        for p in self.assigned_pickups[:]:  # Iterate over a copy of confirmed pickups waiting for this taxi.
            if self.pos == p.pos and len(self.passengers) < self.capacity:  # Check if the passenger is at this cell and seats remain.
                p.status = "in_taxi"  # Mark the passenger as physically riding in the taxi.
                self.assigned_pickups.remove(p)  # Remove the passenger from the waiting pickup list.
                self.passengers.append(p)  # Add the passenger to the active cab manifest.
                self.schedule_stop(p.destination)  # Queue the passenger's drop-off coordinate in the multi-hop route.
                boarded_new = True  # Record that a boarding delay should be applied.
                self.model.log_event(f"🚕 [BOARDED] Taxi #{self.unique_id} picked up Passenger #{p.unique_id} at {self.pos} (Occupancy: {len(self.passengers)}/{self.capacity})")  # Log the pickup.

        return boarded_new  # Return whether a pickup occurred at this stop.

    def step(self):  # Advance the taxi by one simulation step.
        # 1. CFP Protocol: Process incoming CFP and ACCEPT messages.
        cfp_messages = [m for m in self.mailbox if m["type"] == "CFP"]  # Extract all Call For Proposal messages.
        accept_messages = [m for m in self.mailbox if m["type"] == "ACCEPT"]  # Extract all passenger acceptance messages.
        cancel_messages = [m for m in self.mailbox if m["type"] == "CANCEL"]  # Extract booking cancellations.
        self.mailbox.clear()  # Clear the mailbox after extracting current messages.

        for msg in cancel_messages:  # Free the seats of passengers who cancelled before pickup.
            p = msg["passenger"]  # Identify the cancelling passenger.
            if p in self.assigned_pickups:  # Only act if the booking still exists.
                self.assigned_pickups.remove(p)  # Release the reserved seat.
                still_needed = any(q.pos == p.pos for q in self.assigned_pickups) or any(q.destination == p.pos for q in self.passengers)  # Is that stop still useful for someone else?
                if not still_needed and p.pos in self.route_queue:  # Drop a stop that no longer serves anyone.
                    self.route_queue.remove(p.pos)  # Remove the cancelled pickup from the multi-hop route.

        for msg in accept_messages:  # Process acceptances first so seat reservations are locked in.
            p = msg["passenger"]  # Identify the passenger accepting our proposal.
            if self.total_committed < self.capacity and p.status == "awaiting_confirm":  # Verify seat availability and passenger state.
                self.assigned_pickups.append(p)  # Reserve a seat and schedule the passenger for pickup.
                p.mailbox.append({"type": "CONFIRM", "taxi": self, "passenger": p})  # Send a confirmation message back to the passenger.
                self.schedule_stop(p.pos)  # Add the passenger's pickup location to the multi-hop route queue.
            else:  # Handle the case where capacity filled up simultaneously.
                p.mailbox.append({"type": "REJECT", "taxi": self, "passenger": p})  # Notify the passenger to re-broadcast their CFP.

        for msg in cfp_messages:  # Process incoming CFP requests from waiting passengers.
            passenger = msg["sender"]  # Identify the passenger making the request.
            if self.total_committed < self.capacity and self.is_route_compatible(passenger):  # Check capacity and carshare route fit.
                ref_pos = self.target_pos if self.target_pos else self.pos  # Calculate distance from current position or active jump target.
                dist = abs(ref_pos[0] - passenger.origin[0]) + abs(ref_pos[1] - passenger.origin[1])  # Calculate Manhattan pickup distance.
                # Give shared rides a slight priority bonus (-1) when compatible so passengers prefer pooling over pulling a distant empty cab!
                bid_score = max(0, dist - (1 if self.total_committed > 0 else 0))  # Compute bid score favoring nearby carshare pooling.
                passenger.mailbox.append({  # Send a proposal back to the passenger.
                    "type": "PROPOSE",  # Label the response as a proposal.
                    "taxi": self,  # Include this taxi as the proposer.
                    "passenger": passenger,  # Identify the intended passenger.
                    "bid_score": bid_score  # Include the bid score for comparison.
                })  # Finish the proposal message.
                self.model.propose_count_step += 1  # Increment the step counter for proposals sent.
                self.model.log_event(f"📩 [PROPOSE] Taxi #{self.unique_id} at {self.pos} sent proposal to Passenger #{passenger.unique_id} at {passenger.origin} (Bid: {bid_score})")  # Log the proposal.

        # 2. Check for immediate pickups or drop-offs if the taxi is currently idle.
        if self.state == "idle":  # Perform station duties when not mid-jump.
            self.handle_arrival_at_current_pos()  # Drop off and pick up any matching passengers at the current cell.

        # 3. Movement Behavior: Abstract Jumping vs Patrolling.
        if self.state == "traveling":  # Continue an active jump trip.
            self.time_in_transit += 1  # Advance the internal trip clock.
            if self.time_in_transit >= self.trip_duration:  # Check whether the jump duration is complete.
                # INSTANT JUMP to target location.
                old_pos = self.pos  # Remember the starting position for distance accounting.
                self.model.grid.move_agent(self, self.target_pos)  # Move the taxi to its target coordinate on the grid.
                for p in self.passengers:  # Iterate over all passengers currently riding in the taxi.
                    self.model.grid.move_agent(p, self.target_pos)  # Keep aboard passengers' grid positions synced with the taxi.

                jump_dist = abs(self.target_pos[0] - old_pos[0]) + abs(self.target_pos[1] - old_pos[1])  # Calculate the Manhattan jump distance.
                self.distance_traveled += jump_dist  # Add the trip distance to the fuel/pollution metric.

                self.state = "idle"  # Mark the taxi as idle upon reaching the stop.
                self.target_pos = None  # Clear the completed leg's target coordinate.

                boarded_new = self.handle_arrival_at_current_pos()  # Deliver or board passengers at this newly reached stop.

                if self.route_queue:  # Check if additional multi-hop stops remain in the queue.
                    next_stop = self.route_queue.pop(0)  # Pop the next scheduled stop coordinate.
                    self.start_trip(next_stop, extra_delay=1 if boarded_new else 0)  # Start the next leg with a 1-step delay if a new passenger boarded.

        elif self.state == "idle":  # Decide next movement when the taxi is idle.
            if self.route_queue:  # Check if there are stops waiting in the route queue.
                next_stop = self.route_queue.pop(0)  # Pop the next target coordinate.
                if next_stop == self.pos:  # If the stop is already the current coordinate, handle it immediately.
                    self.handle_arrival_at_current_pos()  # Board or drop off at the current cell.
                else:  # Otherwise start jumping toward the target stop.
                    self.start_trip(next_stop)  # Configure and start the jump trip.
            elif self.passengers:  # Fallback check if passengers are aboard without a queued stop.
                self.start_trip(self.passengers[0].destination)  # Start a trip toward the first passenger's destination.
            else:  # Stay parked when idle and wait for CFP requests instead of burning fuel on random patrolling.
                pass  # Parked taxis wait for CFP broadcasts without emitting unnecessary pollution.


class CityTaxiModel(Model):  # Represent the complete city taxi simulation.
    def __init__(self, width=10, height=10, num_taxis=8, num_passengers=25, taxi_capacity=3, allow_route_change=True, seed=42,
                 max_detour=8, walk_steps_per_cell=4, pickup_grace=5, solo_empty_leg=4, verbose=True):  # Configure and populate the model with a reproducible seed.
        super().__init__(rng=seed)  # Initialize Mesa's RNG with the chosen seed so Capacity=1 and Capacity=3 spawn identical cities.
        self.width = width  # Store the grid width.
        self.height = height  # Store the grid height.
        self.taxi_capacity = taxi_capacity  # Store the active taxi capacity for display.
        self.max_detour = max_detour  # Largest extra distance (districts) a taxi accepts to share a ride.
        self.walk_steps_per_cell = walk_steps_per_cell  # Steps a pedestrian needs to cross one district (a taxi needs 1).
        self.pickup_grace = pickup_grace  # Extra patience of a passenger whose seat is already confirmed.
        self.solo_empty_leg = solo_empty_leg  # Assumed empty approach distance per ride in the estimated solo baseline.
        self.verbose = verbose  # Print log lines to the terminal (switched off for batch experiments).
        self.grid = MultiGrid(width, height, torus=False)  # Create a grid whose edges do not wrap around.
        self.step_num = 0  # Track the current simulation step number for logging.
        self.cfp_logs = []  # Store formatted CFP communication strings for the UI log viewer.
        self.cfp_count_step = 0  # Track CFP messages broadcast during the current step.
        self.propose_count_step = 0  # Track PROPOSE messages sent during the current step.
        self.accept_count_step = 0  # Track ACCEPT messages sent during the current step.

        for i in range(num_taxis):  # Create the requested number of taxis.
            x = self.random.randrange(width)  # Choose a seeded random taxi x-coordinate.
            y = self.random.randrange(height)  # Choose a seeded random taxi y-coordinate.
            taxi = Taxi(model=self, capacity=taxi_capacity, allow_route_change=allow_route_change)  # Instantiate a taxi with the configured capacity and routing rule.
            self.grid.place_agent(taxi, (x, y))  # Place the taxi on the grid.

        for i in range(num_passengers):  # Create the requested number of passengers.
            orig_x, orig_y = self.random.randrange(width), self.random.randrange(height)  # Choose a seeded random origin.
            dest_x, dest_y = self.random.randrange(width), self.random.randrange(height)  # Choose a seeded random destination.
            while (orig_x, orig_y) == (dest_x, dest_y):  # Ensure origin and destination differ.
                dest_x, dest_y = self.random.randrange(width), self.random.randrange(height)  # Choose another destination.

            passenger = Passenger(model=self, origin=(orig_x, orig_y), destination=(dest_x, dest_y))  # Instantiate the passenger.
            self.grid.place_agent(passenger, (orig_x, orig_y))  # Place the passenger at the origin.

        self.datacollector = DataCollector(model_reporters=self.metric_definitions())  # Collect every metric after each step.

    @staticmethod
    def metric_definitions():  # One place that defines every observable variable of the study.
        def served(m):  # Passengers who actually boarded a taxi.
            return [p for p in m.agents if is_passenger(p) and p.status in ("in_taxi", "arrived")]

        def arrived(m):  # Passengers delivered by taxi.
            return [p for p in m.agents if is_passenger(p) and p.status == "arrived"]

        def fleet_fuel(m):  # Total distance driven by the fleet = fuel / pollution proxy.
            return sum(t.distance_traveled for t in m.agents if is_taxi(t))

        return {
            # Wait is averaged over passengers who boarded, so people who gave up do not distort it.
            "Avg Wait Time": lambda m: sum(p.wait_time for p in served(m)) / max(1, len(served(m))),
            "Avg Travel Time": lambda m: sum(p.travel_time for p in arrived(m)) / max(1, len(arrived(m))),
            "Arrived": lambda m: len(arrived(m)),
            "Active Negotiations": lambda m: sum(1 for p in m.agents if is_passenger(p) and p.status in ("negotiating", "awaiting_confirm")),
            "Impatient (Walked)": lambda m: sum(1 for p in m.agents if is_passenger(p) and p.status == "impatient"),
            "Reached on Foot": lambda m: sum(1 for p in m.agents if is_passenger(p) and p.reached_on_foot),
            "Unresolved": lambda m: sum(1 for p in m.agents if is_passenger(p) and p.status not in ("arrived", "impatient")),
            "Total Fuel / Pollution (Distance)": fleet_fuel,
            "Fuel per Arrival": lambda m: fleet_fuel(m) / max(1, len(arrived(m))),
            # Estimate only: direct distance + an assumed empty approach leg. The experiments script measures the
            # real solo baseline instead, by running the same city with capacity 1.
            "Est. Solo Fuel (No Carshare)": lambda m: sum(p.direct_distance + m.solo_empty_leg for p in arrived(m)),
        }

    def log_event(self, message):  # Log a communication or movement event to both terminal and UI.
        formatted = f"[Step {self.step_num}] {message}"  # Prefix the event message with the current step number.
        if self.verbose:  # Batch experiments switch printing off to stay fast and readable.
            print(formatted, flush=True)  # Print immediately to the terminal with flush=True so Solara does not buffer it.
        self.cfp_logs.append(formatted)  # Append the message to the model's log list for the web UI.

    def step(self):  # Advance the entire simulation by one tick.
        self.step_num += 1  # Increment the simulation step counter.
        self.cfp_count_step = 0  # Reset per-step CFP broadcast counter.
        self.propose_count_step = 0  # Reset per-step proposal counter.
        self.accept_count_step = 0  # Reset per-step acceptance counter.

        passengers = [a for a in self.agents if isinstance(a, Passenger)]  # Gather all passenger agents.
        taxis = [a for a in self.agents if isinstance(a, Taxi)]  # Gather all taxi agents.
        self.random.shuffle(passengers)  # Randomize passenger turn order.
        self.random.shuffle(taxis)  # Randomize taxi turn order.

        for p in passengers:  # Step all passengers first so CFPs and ACCEPTs enter taxi mailboxes.
            p.step()  # Execute passenger step.
        for t in taxis:  # Step all taxis next so they reply with proposals/confirmations and move.
            t.step()  # Execute taxi step.

        self.datacollector.collect(self)  # Record metrics after agents act so negotiations and movements appear immediately.


# Reactive states for Solara UI.
model_state = solara.reactive(None)  # Hold the currently displayed model.
step_count = solara.reactive(0)  # Hold the number of user-triggered steps.
num_taxis_state = solara.reactive(8)  # Hold the configured taxi count.
num_passengers_state = solara.reactive(25)  # Hold the configured passenger count.
taxi_capacity_state = solara.reactive(3)  # Hold the configured taxi capacity.
route_change_state = solara.reactive(True)  # Hold the toggle state for multi-hop route changes.
seed_state = solara.reactive(42)  # Hold the random seed so comparisons use the exact same passenger/taxi layout.


def init_model():  # Create a fresh simulation using the current UI settings.
    m = CityTaxiModel(  # Build a new city taxi model.
        width=10,  # Set the grid width.
        height=10,  # Set the grid height.
        num_taxis=num_taxis_state.value,  # Use the selected taxi count.
        num_passengers=num_passengers_state.value,  # Use the selected passenger count.
        taxi_capacity=taxi_capacity_state.value,  # Use the selected taxi capacity.
        allow_route_change=route_change_state.value,  # Use the selected route-change rule.
        seed=seed_state.value,  # Use the selected random seed for reproducible comparisons.
    )  # Finish model construction.
    model_state.value = m  # Make the new model visible to Solara.
    step_count.value = 0  # Reset the displayed step number.


@solara.component  # Mark the function as a Solara-rendered component.
def Page():  # Render the simulation controls, charts, and CFP log viewer.
    # Automatically rebuild and reset the model whenever the user changes ANY slider or checkbox!
    solara.use_effect(  # Register a reactive effect that watches all parameter controls.
        init_model,  # Call init_model() automatically whenever a dependency changes.
        [num_taxis_state.value, num_passengers_state.value, taxi_capacity_state.value, route_change_state.value, seed_state.value]  # Watch these 5 controls.
    )  # Finish the auto-reset effect.

    if model_state.value is None:  # Initialize the model on the first render if not yet created.
        init_model()  # Create the initial simulation.

    model = model_state.value  # Read the active model for rendering and callbacks.

    def run_step():  # Advance the simulation once.
        model.step()  # Run one model tick.
        step_count.value += 1  # Update the displayed step number.

    def run_10_steps():  # Advance the simulation ten times.
        for _ in range(10):  # Repeat the model step ten times.
            model.step()  # Run one model tick.
        step_count.value += 10  # Update the displayed step number by 10.

    def reset_model():  # Rebuild the simulation from the selected settings.
        init_model()  # Replace the active model and reset its step counter.

    with solara.Column(style={"padding": "20px", "max-width": "1250px", "margin": "auto"}):  # Create the main centered page container.
        solara.Markdown("# City Taxi Carshare & CFP Communication Simulation ")  # Render the page title.

        # Each control = its label (with the current value) ABOVE a full-width slider. When the label sat on the
        # same line as the slider, a long label used up the whole box and left the track zero pixels wide.
        def labeled_slider(label, state, lo, hi):  # Draw one slider with its label and current value on top.
            with solara.Column(gap="0px", style={"width": "220px"}):
                solara.Text(f"{label}: {state.value}", style={"font-size": "14px", "color": "#555"})
                solara.SliderInt("", value=state, min=lo, max=hi, step=1)

        with solara.Row(style={"flex-wrap": "wrap", "gap": "32px", "align-items": "center"}):  # Wrap onto a second line on small screens.
            labeled_slider("Number of Taxis", num_taxis_state, 2, 20)
            labeled_slider("Number of Passengers", num_passengers_state, 5, 60)
            labeled_slider("Taxi Capacity (1 = solo)", taxi_capacity_state, 1, 4)
            labeled_slider("Random Seed", seed_state, 1, 100)
            solara.Checkbox(label="Allow Multi-Hop Route Changes", value=route_change_state)  # Add the route-change toggle.

        with solara.Row(style={"gap": "10px", "margin-bottom": "15px"}):  # Arrange action buttons in one row.
            solara.Button("Step (+1)", color="primary", on_click=run_step)  # Add the single-step button.
            solara.Button("Run 10 Steps", color="success", on_click=run_10_steps)  # Add the ten-step button.
            solara.Button("Reset Model", color="error", on_click=reset_model)  # Add the reset button.

        # Display a live summary banner showing the exact active capacity, arrived count, and total fuel burned.
        total_fuel = sum(t.distance_traveled for t in model.agents if isinstance(t, Taxi))  # Calculate current total fuel burned.
        arrived_cnt = sum(1 for p in model.agents if isinstance(p, Passenger) and p.status == "arrived")  # Calculate current arrived passengers.
        gave_up_cnt = sum(1 for p in model.agents if isinstance(p, Passenger) and p.status == "impatient")  # Count passengers lost to walking.
        solara.Markdown(f"### Step: {step_count.value} | Capacity: {model.taxi_capacity} | Arrived: {arrived_cnt}/{num_passengers_state.value} | Gave up: {gave_up_cnt} | Fuel: {total_fuel} units")  # Show live KPIs in the header.

        fig, axes = plt.subplots(1, 3, figsize=(16, 4.8))  # Create side-by-side grid, passenger metric, and fuel/pollution charts.

        taxi_x, taxi_y = [], []  # Store coordinates for taxi markers.
        waiting_x, waiting_y = [], []  # Store coordinates for active passenger markers.
        arrived_x, arrived_y = [], []  # Store coordinates for arrived passenger markers.
        impatient_x, impatient_y = [], []  # Store coordinates for impatient walking passenger markers.

        for agent in model.agents:  # Classify every agent for the grid chart.
            if isinstance(agent, Taxi):  # Identify taxi agents.
                taxi_x.append(agent.pos[0])  # Store the taxi's x-coordinate.
                taxi_y.append(agent.pos[1])  # Store the taxi's y-coordinate.
            elif isinstance(agent, Passenger):  # Identify passenger agents.
                if agent.status in ["waiting", "negotiating", "awaiting_confirm", "waiting_pickup", "in_taxi"]:  # Group passengers not yet delivered.
                    waiting_x.append(agent.pos[0])  # Store the active passenger's x-coordinate.
                    waiting_y.append(agent.pos[1])  # Store the active passenger's y-coordinate.
                elif agent.status == "arrived":  # Group passengers who reached their destinations.
                    arrived_x.append(agent.pos[0])  # Store the arrived passenger's x-coordinate.
                    arrived_y.append(agent.pos[1])  # Store the arrived passenger's y-coordinate.
                elif agent.status == "impatient":  # Group passengers who gave up waiting and started walking.
                    impatient_x.append(agent.pos[0])  # Store the impatient passenger's x-coordinate.
                    impatient_y.append(agent.pos[1])  # Store the impatient passenger's y-coordinate.

        axes[0].scatter(taxi_x, taxi_y, color='#e74c3c', marker='s', s=120, label='Taxis', zorder=3, edgecolors='black', linewidths=0.8)  # Plot taxi positions as red squares.
        axes[0].scatter(waiting_x, waiting_y, color='#3498db', marker='o', s=80, label='Active Passengers', zorder=2, edgecolors='black', linewidths=0.6)  # Plot active passengers as blue circles.
        axes[0].scatter(arrived_x, arrived_y, color='#2ecc71', marker='^', s=90, label='Arrived', zorder=1, edgecolors='black', linewidths=0.6)  # Plot delivered passengers as green triangles.
        axes[0].scatter(impatient_x, impatient_y, color='#f39c12', marker='x', s=80, label='Impatient (Walking)', zorder=2)  # Plot walking passengers as orange crosses.

        axes[0].set_xlim(-1, model.width)  # Set the horizontal chart boundaries.
        axes[0].set_ylim(-1, model.height)  # Set the vertical chart boundaries.
        axes[0].set_xticks(range(model.width))  # Show one tick per grid column.
        axes[0].set_yticks(range(model.height))  # Show one tick per grid row.
        axes[0].grid(True, linestyle='--', alpha=0.6)  # Draw a light grid behind the markers.
        axes[0].set_title(f"District Grid (Step {step_count.value}, Cap={model.taxi_capacity})")  # Label the current grid state and capacity.
        axes[0].set_xlabel("District X")  # Label the horizontal axis.
        axes[0].set_ylabel("District Y")  # Label the vertical axis.
        axes[0].legend(loc='upper right', fontsize=8)  # Explain the marker colors and shapes.

        df = model.datacollector.get_model_vars_dataframe()  # Read collected model metrics as a DataFrame.
        if not df.empty and len(df) > 0:  # Plot metrics when at least one sample exists.
            df.index = range(1, len(df) + 1)  # Normalize the chart's x-axis index to start at Step 1.
            df[["Avg Wait Time", "Avg Travel Time", "Arrived", "Active Negotiations", "Impatient (Walked)"]].plot(ax=axes[1], marker="o")  # Draw passenger and CFP metrics.
            axes[1].set_title("Passenger, CFP & Acceptability Metrics")  # Label the passenger metrics chart.
            axes[1].set_xlabel("Steps")  # Label the passenger metrics time axis.
            axes[1].grid(True)  # Draw a grid behind the metric lines.

            df[["Total Fuel / Pollution (Distance)", "Est. Solo Fuel (No Carshare)"]].plot(ax=axes[2], color=["#8e44ad", "#95a5a6"], style=["-", "--"], marker="o")  # Compare actual carshare fuel burned against solo taxi baseline fuel.
            axes[2].set_title("Fuel & Pollution: Carshare vs. Solo Baseline")  # Label the fuel and pollution chart.
            axes[2].set_xlabel("Steps")  # Label the fuel time axis.
            axes[2].set_ylabel("Fuel Units (Grid Cells Traveled)")  # Label the y-axis in fuel units.
            axes[2].grid(True)  # Draw a grid behind the fuel lines.
        else:  # Handle the initial state before any data is collected.
            axes[1].set_title("Run steps to generate passenger & CFP metrics...")  # Tell the user why the middle chart is empty.
            axes[2].set_title("Run steps to generate fuel & pollution metrics...")  # Tell the user why the right chart is empty.

        plt.tight_layout()  # Adjust chart spacing to prevent labels from overlapping.
        solara.FigureMatplotlib(fig)  # Display the Matplotlib figure in Solara.
        plt.close(fig)  # Release the figure after handing it to the UI.

        # --- Live CFP Communication Protocol Log Panel in the Web UI ---
        solara.Markdown("### 📡 Live CFP Communication Log (Latest 25 Messages)")  # Render the header for the live CFP log panel.
        if model.cfp_logs:  # Check whether any CFP messages have been recorded yet.
            recent_logs = "\n".join(model.cfp_logs[-25:])  # Join the 25 most recent log lines into a single block.
            solara.Preformatted(  # Display the logs inside a preformatted monospace box.
                recent_logs,  # Pass the formatted log text.
                style={  # Configure dark-theme terminal styling for the log box.
                    "background-color": "#1e1e1e",  # Use a dark terminal background color.
                    "color": "#d4d4d4",  # Use light gray text for readability.
                    "padding": "12px",  # Add inner padding around the text.
                    "border-radius": "8px",  # Round the corners of the log container.
                    "max-height": "260px",  # Limit the vertical height of the log box.
                    "overflow-y": "auto",  # Enable vertical scrolling when logs exceed the box height.
                    "font-family": "monospace",  # Render logs in a fixed-width terminal font.
                    "font-size": "12.5px",  # Set a clean, readable font size.
                }  # Finish the style dictionary.
            )  # Finish the Preformatted component.
        else:  # Show an instructional banner before Step 1 is run.
            solara.Info("Click 'Step (+1)' or 'Run 10 Steps' to see CFP, PROPOSE, ACCEPT, and CONFIRM messages.")  # Prompt the user to advance the simulation.