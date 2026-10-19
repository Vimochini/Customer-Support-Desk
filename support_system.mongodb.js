// Support desk database. Run once in mongosh or the VS Code MongoDB playground. Safe to run again.
// Creates the collections, their rules, the indexes, and the views the desk reads. Adds no data.
use('support_system');

const collections = {
  users: {
    validator: {
      $jsonSchema: {
        bsonType: 'object',
        required: ['firstName', 'lastName', 'email', 'role'],
        properties: {
          firstName: { bsonType: 'string', minLength: 1 },
          lastName: { bsonType: 'string', minLength: 1 },
          email: { bsonType: 'string', pattern: '^\\S+@\\S+\\.\\S+$' },
          role: { enum: ['customer', 'agent'] },
        },
      },
    },
  },
  tickets: {
    validator: {
      $jsonSchema: {
        bsonType: 'object',
        required: ['ticket_no', 'subject', 'description', 'status', 'customer_id'],
        properties: {
          ticket_no: { bsonType: 'number' },
          subject: { bsonType: 'string', minLength: 1 },
          description: { bsonType: 'string', minLength: 1 },
          status: { enum: ['Open', 'In Progress', 'Pending', 'Resolved', 'Closed'] },
          customer_id: { bsonType: 'objectId' },
          priority: {
            bsonType: 'object',
            properties: {
              level: { enum: ['Low', 'Medium', 'High', 'Critical'] },
              score: { bsonType: 'number', minimum: 1, maximum: 5 },
            },
          },
          comments: {
            bsonType: 'array',
            items: {
              bsonType: 'object',
              required: ['author_id', 'message', 'timestamp'],
              properties: {
                author_id: { bsonType: 'objectId' },
                message: { bsonType: 'string', minLength: 1 },
                timestamp: { bsonType: 'date' },
              },
            },
          },
        },
      },
    },
  },
};

for (const [name, options] of Object.entries(collections)) {
  if (!db.getCollectionNames().includes(name)) db.createCollection(name);
  if (options.validator) db.runCommand({ collMod: name, validator: options.validator });
}

// One email per person, and at most one agent (the desk has a single agent)
db.users.createIndex({ email: 1 }, { unique: true });
db.users.createIndex({ role: 1 }, { unique: true, partialFilterExpression: { role: 'agent' } });
db.tickets.createIndex({ ticket_no: 1 }, { unique: true });

// The one agent (you). Put your name here before the first run. Later runs keep the stored agent unchanged.
db.users.updateOne(
  { role: 'agent' },
  { $setOnInsert: { firstName: 'Sam', lastName: 'Support', email: 'agent@desk.example', role: 'agent' } },
  { upsert: true }
);

// Views: read-only lists the desk shows. Recreated on every run.
for (const view of ['ticket_board', 'ticket_counts']) {
  if (db.getCollectionNames().includes(view)) db[view].drop();
}

// Each ticket with the customer's name and the priority level, sorted by ticket number
db.createView('ticket_board', 'tickets', [
  { $lookup: { from: 'users', localField: 'customer_id', foreignField: '_id', as: 'customer' } },
  { $unwind: '$customer' },
  {
    $project: {
      ticket_no: 1,
      subject: 1,
      description: 1,
      status: 1,
      priority: '$priority.level',
      comments: 1,
      customer: { $concat: ['$customer.firstName', ' ', '$customer.lastName'] },
    },
  },
  { $sort: { ticket_no: 1 } },
]);

// Number of tickets in each status
db.createView('ticket_counts', 'tickets', [
  { $group: { _id: '$status', count: { $sum: 1 } } },
]);
